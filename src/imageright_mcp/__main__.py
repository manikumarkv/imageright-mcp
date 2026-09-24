"""Console entry point: ``imageright-mcp`` or ``python -m imageright_mcp`` runs the stdio server."""

from imageright_mcp.server import create_server


def main() -> None:
    create_server().run("stdio")


if __name__ == "__main__":
    main()
