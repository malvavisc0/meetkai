import pytest

from kai.config.prompts import load_system_prompt


class TestLoadSystemPrompt:
    def test_txt_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("You are a helpful assistant.")
        assert load_system_prompt(f) == "You are a helpful assistant."

    def test_md_file(self, tmp_path):
        f = tmp_path / "prompt.md"
        f.write_text("# System Prompt\nBe helpful.")
        assert "# System Prompt" in load_system_prompt(f)

    def test_prompt_extension(self, tmp_path):
        f = tmp_path / "prompt.prompt"
        f.write_text("Custom prompt format.")
        assert load_system_prompt(f) == "Custom prompt format."

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("  \n  Hello  \n  ")
        assert load_system_prompt(f) == "Hello"

    def test_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_system_prompt(tmp_path / "nonexistent.txt")

    def test_unsupported_extension(self, tmp_path):
        f = tmp_path / "prompt.json"
        f.write_text("{}")
        with pytest.raises(ValueError, match="Unsupported"):
            load_system_prompt(f)

    def test_empty_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("")
        with pytest.raises(ValueError, match="empty"):
            load_system_prompt(f)

    def test_whitespace_only_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("   \n  \t  ")
        with pytest.raises(ValueError, match="empty"):
            load_system_prompt(f)

    def test_directory_path(self, tmp_path):
        d = tmp_path / "subdir"
        d.mkdir()
        with pytest.raises(ValueError, match="not a file"):
            load_system_prompt(d)

    def test_binary_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_bytes(b"\x80\x81\x82\x83")
        with pytest.raises(ValueError, match="UTF-8"):
            load_system_prompt(f)

    def test_unicode_content(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("Tú eres un asistente útil. 你好")
        result = load_system_prompt(f)
        assert "你好" in result

    def test_large_file(self, tmp_path):
        f = tmp_path / "prompt.txt"
        content = "A" * 100_000
        f.write_text(content)
        assert len(load_system_prompt(f)) == 100_000

    def test_string_path(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("Test")
        assert load_system_prompt(str(f)) == "Test"
