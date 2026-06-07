---
name: feature-dev
version: 1.0.0
description: 功能开发流程技能 — 从 SPEC 出发，完成代码实现，同步更新 FEATURES 状态。确保每次开发都符合设计约束且不留状态遗漏。
author: dev
tags: [process, development, spec, features]

triggers:
  keywords: [开发功能, 实现功能, 开发, feature, 写代码, implement, 搭建]
  intents: []

depends_on:
  skills: []
  python_packages: []

tools: []
---

# 功能开发流程

## 执行步骤

当用户要求开发某个功能时，严格按以下顺序执行：

### Step 1 — 读取 SPEC 设计

1. 根据用户指定的功能点，在 `SPEC.md` 中搜索对应章节
2. 阅读完整设计：接口定义 / 数据结构 / 依赖关系 / 边界条件
3. 如果 SPEC 中的设计不足够清晰，向用户提问澄清
4. 如果发现 SPEC 设计有明显问题，提出修改建议，用户确认后先改 SPEC

### Step 2 — 确认 FEATURES 状态

1. 在 `FEATURES.md` 中找到对应的功能编号
2. 确认状态为「待开发」
3. 如果状态不对，告警用户
4. 确认该功能依赖的前置功能是否已完成

### Step 3 — 确认实现方案

1. 列出将要创建/修改的文件清单
2. 列出将要实现的关键类/函数/方法
3. 口头向用户确认方案概要（2-3 句即可）
4. 开始编写代码

### Step 4 — 编写代码

1. 遵循 `CLAUDE.md` 中的代码规范
2. 使用 SPEC 中指定的类型和接口
3. 每个新文件包含完整的类型注解
4. 对外暴露的类/方法写简短的 docstring

### Step 5 — 更新 FEATURES 状态

代码完成后**立即**更新 `FEATURES.md`：
- 将对应功能点的状态从「待开发」改为「开发完成」

### Step 6 — 编写测试（如果用户要求）

1. 在 `tests/` 对应目录下创建测试文件
2. 测试完成后将 FEATURES 状态从「开发完成」改为「测试完成」

### Step 7 — 同步 SPEC（如果需要）

如果开发过程中对设计做了调整：
1. 更新 `SPEC.md` 中对应章节
2. 简要告知用户 SPEC 变更了什么

## 约束

- 禁止跳过 SPEC 阅读直接写代码
- 禁止修改代码后不更新 FEATURES.md
- SPEC 和 FEATURES 有任何不一致时，优先确认 SPEC 是否正确再改代码
