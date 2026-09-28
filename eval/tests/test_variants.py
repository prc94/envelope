from pathlib import Path

from envelope_eval.runner.variants import build_variant


def test_build_variant_records_hash(tmp_path: Path):
    patch = tmp_path / "variant.patch"
    patch.write_text(
        """diff --git a/envelope/output/render.py b/envelope/output/render.py\n--- a/envelope/output/render.py\n+++ b/envelope/output/render.py\n@@ -12,1 +12,1 @@\n-HEAD_SHARE = 1 / 3\n+HEAD_SHARE = 1 / 2\n""",
        encoding="utf-8",
    )
    result = build_variant(Path.cwd(), patch, name="head-tail", output=tmp_path / "variant.pyz")
    assert (
        result.artifact.exists()
        and len(result.source_hash) == 64
        and len(result.artifact_hash) == 64
    )


def test_a_relative_output_path_and_the_shipped_head_half_patch(tmp_path: Path, monkeypatch):
    """The build runs from a temporary directory: a relative --out must still end up where asked,
    and the head/tail patch the M1 job uses must apply and change the split."""
    monkeypatch.chdir(tmp_path)
    repo = Path(__file__).parents[2]
    result = build_variant(
        repo,
        repo / "eval/variants/head-half.patch",
        name="head-half",
        output=Path("rel/head-half.pyz"),
    )
    assert (
        result.artifact == (tmp_path / "rel" / "head-half.pyz").resolve()
        and result.artifact.exists()
    )
    default = build_variant(repo, None, name="default", output=Path("rel/default.pyz"))
    assert default.artifact.exists() and default.artifact_hash != result.artifact_hash
    assert default.source_hash != result.source_hash  # the patch changed the source
