# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Runner Manager manages the runners on LXD and GitHub."""

from __future__ import annotations

import hashlib
import logging
import tarfile
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional

import fastcore.net
import jinja2
import requests
import requests.adapters
import urllib3
from ghapi.all import GhApi
from typing_extensions import assert_never

from errors import RunnerBinaryError, RunnerCreateError
from github_type import (
    GitHubRunnerStatus,
    RegistrationToken,
    RemoveToken,
    RunnerApplication,
    RunnerApplicationList,
    SelfHostedRunner,
)
from lxd import LxdClient, LxdInstance
from repo_policy_compliance_client import RepoPolicyComplianceClient
from runner import Runner, RunnerClients, RunnerConfig, RunnerStatus
from runner_type import GitHubOrg, GitHubPath, GitHubRepo, ProxySetting, VirtualMachineResources
from utilities import retry, set_env_var

logger = logging.getLogger(__name__)


@dataclass
class RunnerManagerConfig:
    """Configuration of runner manager.

    Attrs:
        path: GitHub repository path in the format '<owner>/<repo>', or the GitHub organization
            name.
        token: GitHub personal access token to register runner to the repository or
            organization.
        image: Name of the image for creating LXD instance.
        service_token: Token for accessing local service.
        lxd_storage_path: Path to be used as LXD storage.
    """

    path: GitHubPath
    token: str
    image: str
    service_token: str
    lxd_storage_path: Path


@dataclass
class RunnerInfo:
    """Information from GitHub of a runner.

    Used as a returned type to method querying runner information.
    """

    name: str
    status: GitHubRunnerStatus


class RunnerManager:
    """Manage a group of runners according to configuration."""

    runner_bin_path = Path("/home/ubuntu/github-runner-app")

    def __init__(
        self,
        app_name: str,
        unit: int,
        runner_manager_config: RunnerManagerConfig,
        proxies: ProxySetting = ProxySetting(),
    ) -> None:
        """Construct RunnerManager object for creating and managing runners.

        Args:
            app_name: An name for the set of runners.
            unit: Unit number of the set of runners.
            runner_manager_config: Configuration for the runner manager.
            proxies: HTTP proxy settings.
        """
        self.app_name = app_name
        self.instance_name = f"{app_name}-{unit}"
        self.config = runner_manager_config
        self.proxies = proxies

        # Setting the env var to this process and any child process spawned.
        if "no_proxy" in self.proxies:
            set_env_var("NO_PROXY", self.proxies["no_proxy"])
        if "http" in self.proxies:
            set_env_var("HTTP_PROXY", self.proxies["http"])
        if "https" in self.proxies:
            set_env_var("HTTPS_PROXY", self.proxies["https"])

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=urllib3.Retry(
                total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]
            )
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        if self.proxies:
            # setup proxy for requests
            self.session.proxies.update(self.proxies)
            # add proxy to fastcore which ghapi uses
            proxy = urllib.request.ProxyHandler(self.proxies)
            opener = urllib.request.build_opener(proxy)
            fastcore.net._opener = opener

        # The repo policy compliance service is on localhost and should not have any proxies
        # setting configured. The is a separated requests Session as the other one configured
        # according proxies setting provided by user.
        local_session = requests.Session()
        local_session.mount("http://", adapter)
        local_session.mount("https://", adapter)
        local_session.trust_env = False

        self._clients = RunnerClients(
            GhApi(token=self.config.token),
            jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=True),
            LxdClient(),
            RepoPolicyComplianceClient(
                local_session, "http://127.0.0.1:8080", self.config.service_token
            ),
        )

    @retry(tries=5, delay=30, local_logger=logger)
    def get_latest_runner_bin_url(
        self, os_name: str = "linux", arch_name: str = "x64"
    ) -> RunnerApplication:
        """Get the URL for the latest runner binary.

        The runner binary URL changes when a new version is available.

        Args:
            os_name: Name of operating system.
            arch_name: Name of architecture.

        Returns:
            Information on the runner application.
        """
        runner_bins: RunnerApplicationList = []
        if isinstance(self.config.path, GitHubRepo):
            runner_bins = self._clients.github.actions.list_runner_applications_for_repo(
                owner=self.config.path.owner, repo=self.config.path.repo
            )
        if isinstance(self.config.path, GitHubOrg):
            runner_bins = self._clients.github.actions.list_runner_applications_for_org(
                org=self.config.path.org
            )

        logger.debug("Response of runner binary list: %s", runner_bins)

        try:
            return next(
                bin
                for bin in runner_bins
                if bin["os"] == os_name and bin["architecture"] == arch_name
            )
        except StopIteration as err:
            raise RunnerBinaryError(
                f"Unable query GitHub runner binary information for {os_name} {arch_name}"
            ) from err

    @retry(tries=5, delay=30, local_logger=logger)
    def update_runner_bin(self, binary: RunnerApplication) -> None:
        """Download a runner file, replacing the current copy.

        Remove the existing runner binary to prevent it from being used. This
        is done to prevent security issues arising from outdated runner binary
        containing security flaws. The newest version of runner binary should
        always be used.

        Args:
            binary: Information on the runner binary to download.
        """
        logger.info("Downloading runner binary from: %s", binary["download_url"])

        try:
            # Delete old version of runner binary.
            RunnerManager.runner_bin_path.unlink(missing_ok=True)
        except OSError as err:
            logger.exception("Unable to perform file operation on the runner binary path")
            raise RunnerBinaryError("File operation failed on the runner binary path") from err

        try:
            # Download the new file
            response = self.session.get(binary["download_url"], stream=True)

            logger.info(
                "Download of runner binary from %s return status code: %i",
                binary["download_url"],
                response.status_code,
            )

            if not binary["sha256_checksum"]:
                logger.error("Checksum for runner binary is not found, unable to verify download.")
                raise RunnerBinaryError(
                    "Checksum for runner binary is not found in GitHub response."
                )

            sha256 = hashlib.sha256()

            with RunnerManager.runner_bin_path.open(mode="wb") as file:
                # Process with chunk_size of 128 KiB.
                for chunk in response.iter_content(chunk_size=128 * 1024, decode_unicode=False):
                    file.write(chunk)

                    sha256.update(chunk)
        except requests.RequestException as err:
            logger.exception("Failed to download of runner binary")
            raise RunnerBinaryError("Failed to download runner binary") from err

        logger.info("Finished download of runner binary.")

        # Verify the checksum if checksum is present.
        if binary["sha256_checksum"] != sha256.hexdigest():
            logger.error(
                "Expected hash of runner binary (%s) doesn't match the calculated hash (%s)",
                binary["sha256_checksum"],
                sha256,
            )
            raise RunnerBinaryError("Checksum mismatch for downloaded runner binary")

        # Verify the file integrity.
        if not tarfile.is_tarfile(file.name):
            logger.error("Failed to decompress downloaded GitHub runner binary.")
            raise RunnerBinaryError("Downloaded runner binary cannot be decompressed.")

        logger.info("Validated newly downloaded runner binary and enabled it.")

    def get_github_info(self) -> Iterator[RunnerInfo]:
        """Get information on the runners from GitHub.

        Returns:
            List of information from GitHub on runners.
        """
        remote_runners = self._get_runner_github_info()
        return iter(RunnerInfo(runner.name, runner.status) for runner in remote_runners.values())

    def reconcile(self, quantity: int, resources: VirtualMachineResources) -> int:
        """Bring runners in line with target.

        Args:
            quantity: Number of intended runners.
            resources: Configuration of the virtual machine resources.

        Returns:
            Difference between intended runners and actual runners.
        """
        runners = self._get_runners()

        # Add/Remove runners to match the target quantity
        online_runners = [
            runner for runner in runners if runner.status.exist and runner.status.online
        ]

        offline_runners = [runner for runner in runners if not runner.status.online]

        local_runners = {
            instance.name: instance
            # Pylint cannot find the `all` method.
            for instance in self._clients.lxd.instances.all()  # pylint: disable=no-member
            if instance.name.startswith(f"{self.instance_name}-")
        }

        logger.info(
            (
                "Expected runner count: %i, Online runner count: %i, Offline runner count: %i, "
                "LXD instance count: %i"
            ),
            quantity,
            len(online_runners),
            len(offline_runners),
            len(local_runners),
        )

        # Clean up offline runners
        if offline_runners:
            logger.info("Cleaning up offline runners.")

            remove_token = self._get_github_remove_token()

            for runner in offline_runners:
                runner.remove(remove_token)
                logger.info("Removed runner: %s", runner.config.name)

        delta = quantity - len(online_runners)
        # Spawn new runners
        if delta > 0:
            if RunnerManager.runner_bin_path is None:
                raise RunnerCreateError("Unable to create runner due to missing runner binary.")

            logger.info("Getting registration token for GitHub runners.")

            registration_token = self._get_github_registration_token()
            remove_token = self._get_github_remove_token()

            logger.info("Adding %i additional runner(s).", delta)
            for _ in range(delta):
                config = RunnerConfig(
                    self.app_name,
                    self.config.path,
                    self.proxies,
                    self.config.lxd_storage_path,
                    self._generate_runner_name(),
                )
                runner = Runner(self._clients, config, RunnerStatus())
                try:
                    runner.create(
                        self.config.image,
                        resources,
                        RunnerManager.runner_bin_path,
                        registration_token,
                    )
                    logger.info("Created runner: %s", runner.config.name)
                except RunnerCreateError:
                    logger.error("Unable to create runner: %s", runner.config.name)
                    runner.remove(remove_token)
                    logger.info("Cleaned up runner: %s", runner.config.name)
                    raise

        elif delta < 0:
            # Idle runners are online runners that has not taken a job.
            idle_runners = [runner for runner in online_runners if not runner.status.busy]
            offset = min(-delta, len(idle_runners))
            if offset != 0:
                logger.info("Removing %i runner(s).", offset)
                remove_runners = idle_runners[:offset]

                logger.info("Cleaning up idle runners.")

                remove_token = self._get_github_remove_token()

                for runner in remove_runners:
                    runner.remove(remove_token)
                    logger.info("Removed runner: %s", runner.config.name)

            else:
                logger.info("There are no idle runner to remove.")
        else:
            logger.info("No changes to number of runner needed.")

        return delta

    def flush(self, flush_busy: bool = True) -> int:
        """Remove existing runners.

        Args:
            flush_busy: Whether to flush busy runners as well.

        Returns:
            Number of runner removed.
        """
        if flush_busy:
            runners = [runner for runner in self._get_runners() if runner.status.exist]
        else:
            runners = [
                runner
                for runner in self._get_runners()
                if runner.status.exist and not runner.status.busy
            ]

        logger.info("Removing existing %i local runners", len(runners))

        remove_token = self._get_github_remove_token()

        for runner in runners:
            runner.remove(remove_token)
            logger.info("Removed runner: %s", runner.config.name)

        return len(runners)

    def _generate_runner_name(self) -> str:
        """Generate a runner name based on charm name.

        Returns:
            Generated name of runner.
        """
        suffix = str(uuid.uuid4())
        return f"{self.instance_name}-{suffix}"

    def _get_runner_github_info(self) -> Dict[str, SelfHostedRunner]:
        remote_runners_list: list[SelfHostedRunner] = []
        if isinstance(self.config.path, GitHubRepo):
            remote_runners_list = self._clients.github.actions.list_self_hosted_runners_for_repo(
                owner=self.config.path.owner, repo=self.config.path.repo
            )["runners"]
        if isinstance(self.config.path, GitHubOrg):
            remote_runners_list = self._clients.github.actions.list_self_hosted_runners_for_org(
                org=self.config.path.org
            )["runners"]

        logger.debug("List of runners found on GitHub:%s", remote_runners_list)

        return {
            runner.name: runner
            for runner in remote_runners_list
            if runner.name.startswith(f"{self.instance_name}-")
        }

    def _get_runners(self) -> list[Runner]:
        """Query for the list of runners.

        Returns:
            List of `Runner` from information on LXD or GitHub.
        """

        def create_runner_info(
            name: str,
            local_runner: Optional[LxdInstance],
            remote_runner: Optional[SelfHostedRunner],
        ) -> Runner:
            """Create runner from information from GitHub and LXD."""
            logger.debug(
                (
                    "Found runner %s with GitHub info [status: %s, busy: %s, labels: %s] and LXD "
                    "info [status: %s]"
                ),
                name,
                getattr(remote_runner, "status", None),
                getattr(remote_runner, "busy", None),
                getattr(remote_runner, "labels", None),
                getattr(local_runner, "status", None),
            )

            runner_id = getattr(remote_runner, "id", None)
            running = local_runner is not None
            online = getattr(remote_runner, "status", None) == "online"
            busy = getattr(remote_runner, "busy", None)

            config = RunnerConfig(
                self.app_name, self.config.path, self.proxies, self.config.lxd_storage_path, name
            )
            return Runner(
                self._clients,
                config,
                RunnerStatus(runner_id, running, online, busy),
                local_runner,
            )

        remote_runners = self._get_runner_github_info()
        local_runners = {
            instance.name: instance
            # Pylint cannot find the `all` method.
            for instance in self._clients.lxd.instances.all()  # pylint: disable=no-member
            if instance.name.startswith(f"{self.instance_name}-")
        }

        runners: list[Runner] = []
        for name in set(local_runners.keys()) | set(remote_runners.keys()):
            runners.append(
                create_runner_info(name, local_runners.get(name), remote_runners.get(name))
            )

        return runners

    def _get_github_registration_token(self) -> str:
        """Get token from GitHub used for registering runners.

        Returns:
            The registration token.
        """
        token: RegistrationToken
        if isinstance(self.config.path, GitHubRepo):
            token = self._clients.github.actions.create_registration_token_for_repo(
                owner=self.config.path.owner, repo=self.config.path.repo
            )
        elif isinstance(self.config.path, GitHubOrg):
            token = self._clients.github.actions.create_registration_token_for_org(
                org=self.config.path.org
            )
        else:
            assert_never(token)

        return token["token"]

    def _get_github_remove_token(self) -> str:
        """Get token from GitHub used for removing runners.

        Returns:
            The removing token.
        """
        token: RemoveToken
        if isinstance(self.config.path, GitHubRepo):
            token = self._clients.github.actions.create_remove_token_for_repo(
                owner=self.config.path.owner, repo=self.config.path.repo
            )
        elif isinstance(self.config.path, GitHubOrg):
            token = self._clients.github.actions.create_remove_token_for_org(
                org=self.config.path.org
            )
        else:
            assert_never(token)

        return token["token"]
