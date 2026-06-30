"""将 SPEC.md 和 FEATURES.md 拆分为独立文件。"""
import re, os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)


def split_spec():
    src = os.path.join(ROOT, "SPEC.md")
    dest_dir = os.path.join(ROOT, "spec")
    os.makedirs(dest_dir, exist_ok=True)

    with open(src, "r", encoding="utf-8") as f:
        content = f.read()

    chapters = list(re.finditer(r"^## (\d+)\. (.+)$", content, re.MULTILINE))

    spec_map = {
        "1": ("01-overview", "概述"),
        "2": ("02-architecture", "系统架构"),
        "3": ("03-core-components", "核心组件详细设计"),
        "4": ("05-tech-stack", "技术栈选型"),
        "5": ("06-api-design", "API 设计"),
        "6": ("07-data-flow", "数据流示例"),
        "7": ("08-security", "安全设计"),
        "8": ("09-roadmap", "实现路线图"),
        "9": ("10-design-decisions", "关键设计决策"),
        "10": ("11-directory-structure", "目录结构建议"),
        "11": ("12-langgraph-integration", "LangGraph 集成细节"),
        "12": ("13-prompt-templates", "Prompt 模板管理"),
        "13": ("14-evaluation", "评估指标与质量保障"),
        "14": ("15-design-review", "设计审查与风险缓解"),
    }

    entries = []
    for i, m in enumerate(chapters):
        num = m.group(1)
        title = m.group(2)
        start = m.start()
        end = chapters[i + 1].start() if i + 1 < len(chapters) else len(content)
        text = content[start:end].strip()
        target = spec_map.get(num)
        slug, short = target if target else (f"extra-{num}", title)
        filename = f"{slug}.md"
        with open(os.path.join(dest_dir, filename), "w", encoding="utf-8") as f:
            f.write(f"# {num}. {short}\n\n{text}\n")
        entries.append((slug, num, short, text.count("\n") + 1))
        print(f"  spec/{filename}")

    lines = ["# SPEC 技术规格", "", "> 由 SPEC.md 拆分，按章节独立维护。", ""]
    lines.append("| 文件 | 章节 | 行数 |")
    lines.append("|------|------|------|")
    for slug, num, title, lc in entries:
        lines.append(f"| [{slug}.md]({slug}.md) | {num}. {title} | {lc} 行 |")
    lines.append("")
    with open(os.path.join(dest_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  spec/README.md")


def split_features():
    src = os.path.join(ROOT, "FEATURES.md")
    dest_dir = os.path.join(ROOT, "features")
    os.makedirs(dest_dir, exist_ok=True)

    with open(src, "r", encoding="utf-8") as f:
        content = f.read()

    chapters = list(re.finditer(r"^## (\d+)\.", content, re.MULTILINE))
    # 过滤掉不属于章节的数字 ## (如 SQL 中的 ##)
    chapters = [m for m in chapters if int(m.group(1)) <= 100]

    feat_map = {
        "1": ("01-infrastructure", "项目基础设施"), "2": ("02-datasource", "数据源管理"),
        "3": ("03-connectors", "数据库连接器"), "4": ("04-graph", "LangGraph 编排引擎"),
        "5": ("05-tools", "工具层"), "6": ("06-knowledge", "知识库管理"),
        "7": ("07-memory", "记忆系统"), "8": ("08-mcp", "MCP 集成"),
        "9": ("09-skills", "Skills 技能系统"), "10": ("10-llm", "LLM 管理层"),
        "11": ("11-api", "API 层"), "12": ("12-security", "安全模块"),
        "13": ("13-analytics", "数据分析引擎"), "14": ("14-visualization", "可视化引擎"),
        "15": ("15-evaluation", "评估与质量保障"), "16": ("16-testing", "测试"),
        "17": ("17-ops", "基础设施与运维"), "18": ("18-frontend", "前端"),
        "19": ("19-extensions", "扩展能力"),
    }

    entries = []
    for i, m in enumerate(chapters):
        num = m.group(1)
        # 提取 title: 从 "## N. title" 中获取
        line = content[m.start():].split("\n")[0]
        title = re.sub(r"^## \d+\.\s*", "", line).strip()
        # 去掉优先级标记
        title = re.sub(r"\s*`\[.*?\]`$", "", title).strip()
        start = m.start()
        end = chapters[i + 1].start() if i + 1 < len(chapters) else len(content)
        text = content[start:end].strip()
        target = feat_map.get(num)
        slug, short = target if target else (f"extra-{num}", title)
        filename = f"{slug}.md"
        with open(os.path.join(dest_dir, filename), "w", encoding="utf-8") as f:
            f.write(f"# {num}. {short}\n\n{text}\n")
        entries.append((slug, num, short, text.count("\n") + 1))
        print(f"  features/{filename}")

    appendix_start = content.find("\n## 优先级分配总览")
    if appendix_start > 0:
        text = content[appendix_start:].strip()
        filename = "99-summary.md"
        with open(os.path.join(dest_dir, filename), "w", encoding="utf-8") as f:
            f.write("# 附录\n\n" + text)
        entries.append(("99-summary", "", "附录", text.count("\n") + 1))
        print(f"  features/{filename}")

    lines = ["# FEATURES 功能清单", "", "> 由 FEATURES.md 拆分，按模块独立维护。", ""]
    lines.append("| 文件 | 章节 | 行数 |")
    lines.append("|------|------|------|")
    for slug, num, title, lc in entries:
        label = f"{num}. {title}" if num else title
        lines.append(f"| [{slug}.md]({slug}.md) | {label} | {lc} 行 |")
    lines.append("")
    with open(os.path.join(dest_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  features/README.md")


if __name__ == "__main__":
    print("Splitting SPEC.md...")
    split_spec()
    print("Splitting FEATURES.md...")
    split_features()
    print("Done!")
