from envelope._pycheck import ensure_supported  # pyright: ignore[reportUnknownVariableType]

ensure_supported()

from envelope.cli import main  # noqa: E402

raise SystemExit(main())
