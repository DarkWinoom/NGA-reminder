"""项目内容不许指向本地工作区。

评估文档、设计稿、决策记录都放在 ``.local-work/``——那是**过程材料**，
不进版本库。于是有一条必须一直守着的规矩：

    项目里的任何内容（README、源码、注释、配置）都不能引用它们。

理由很实在：仓库里放一个指向不存在文件的链接，读者点开就是 404，
比没有链接更糟。

另外顺带守住两条：

- README 里引用的图片必须真的存在；
- README 要把三种跑法（控制台 / Docker / 通知桥接）和三个系统都说到。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 项目里出现这些片段，就说明有人在指路到本地工作区。
FORBIDDEN = ("docs/", "design/", ".local-work")

#: 会被扫到的文件类型（够覆盖所有会写文档和路径的地方）。
SCANNED_SUFFIXES = {".md", ".py", ".js", ".css", ".html", ".toml", ".yml", ".ps1", ".cmd"}

#: 这些目录不是项目自己的内容，别扫。
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".local-work",
    "data",
}


def _project_files() -> list[Path]:
    files: list[Path] = []
    # 根目录只扫顶层文件；子目录才递归。
    candidates: list[Path] = [p for p in ROOT.iterdir() if p.is_file()]
    for folder in (ROOT / "src", ROOT / "scripts", ROOT / "tests", ROOT / "assets"):
        if folder.is_dir():
            candidates.extend(p for p in folder.rglob("*") if p.is_file())
    for path in candidates:
        if path.suffix.lower() not in SCANNED_SUFFIXES:
            continue
        # 本文件自己就写着这些关键词——跳过它。
        if path.resolve() == Path(__file__).resolve():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        files.append(path)
    return files


def test_the_project_does_not_point_at_the_local_workspace():
    """项目内容不能引用 .local-work/ 里的任何东西。"""
    offenders: list[str] = []
    for path in _project_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)} → {needle}")
    assert not offenders, "这些内容指向了不进版本库的文件：" + "; ".join(offenders)


def test_the_local_workspace_is_ignored():
    """.local-work/ 必须在 .gitignore 里。"""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".local-work" in ignore


def test_the_readme_images_exist():
    """README 里写的图片路径要真的在仓库里。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for line in readme.splitlines():
        marker = line.find("](")
        if marker == -1:
            continue
        target = line[marker + 2 :].split(")", 1)[0].split(" ", 1)[0]
        if not target or target.startswith(("http://", "https://", "#")):
            continue
        if not (ROOT / target).exists():
            missing.append(target)
    assert not missing, "README 引用了不存在的图片：" + ", ".join(missing)


def test_the_readme_covers_the_three_ways_to_run():
    """三种跑法 + 三个系统，README 里都要说得到。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in (
        "uv run nga-reminder",
        "docker compose up -d",
        "notify_bridge.py",
        "Windows",
        "macOS",
        "Linux",
    ):
        assert needle in readme, needle


def test_the_readme_is_short_and_picture_heavy():
    """新 README 要言简意赅 + 图文并茂，别又写成长文档。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = readme.splitlines()
    assert len(lines) < 220, f"README 太长了：{len(lines)} 行"
    pictures = readme.count("![")
    assert pictures >= 3, "README 里的图太少了"


# ------------------------------------------------------- 抓取边界的文档


def test_the_compliance_notes_are_present_and_linked():
    """README 要有「抓取频率与使用边界」一节，并链到完整版；官方出处要在。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "抓取频率与使用边界" in readme
    assert "(COMPLIANCE.md)" in readme
    assert (ROOT / "COMPLIANCE.md").exists()

    compliance = (ROOT / "COMPLIANCE.md").read_text(encoding="utf-8")
    for url in (
        "https://bbs.nga.cn/misc/legal/1/agreement.html",
        "https://bbs.nga.cn/read.php?tid=22586501",
        "https://bbs.nga.cn/robots.txt",
    ):
        assert url in readme, url
        assert url in compliance, url


def test_the_documented_pacing_matches_the_code():
    """文档里写的抓取节奏必须跟代码里的真值一致——否则是在骗人。

    数字从代码里取（配置项的**声明默认值**、界面档位），不是写死在测试里。
    注意读 ``model_fields`` 而不是实例化 ``Settings()``——后者会把用户自己
    ``.env`` 里的值读进来，测试结果就会随本机配置飘。
    """
    import re

    from nga_reminder.config import Settings

    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compliance = (ROOT / "COMPLIANCE.md").read_text(encoding="utf-8")

    # 请求间隔：两个文档都要写，且跟声明默认值一致
    assert f"{defaults['delay_ms']} ms" in readme
    assert f"{defaults['delay_ms']} ms" in compliance
    # 429/5xx 的重试次数
    assert f"重试 **{defaults['max_retries']}** 次" in compliance

    # 界面允许的最小轮询间隔
    js = (ROOT / "src" / "nga_reminder" / "web" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const INTERVAL_LABELS = \{(.*?)\};", js, re.S)
    assert block, "app.js 里找不到 INTERVAL_LABELS"
    values = [int(m) for m in re.findall(r"(\d+)\s*:", block.group(1))]
    smallest = min(v for v in values if v > 0)
    assert f"{smallest} 秒" in readme
    assert f"{smallest} 秒" in compliance
