from solidlsp.ls import SolidLanguageServer
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo


class BslLanguageServer(SolidLanguageServer):
    def __init__(self, config, repository_root_path, solidlsp_settings):
        cmd = [
            "java",
            "-Xmx2g",
            "-jar",
            "/opt/bsl-language-server/bsl-language-server.jar",
            "--lsp"
        ]

        launch_info = ProcessLaunchInfo(
            cmd=cmd,
            cwd=repository_root_path,
        )

        super().__init__(
            config,
            repository_root_path,
            launch_info,
            "bsl",
            solidlsp_settings
        )
