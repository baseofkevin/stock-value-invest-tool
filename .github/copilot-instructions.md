# Copilot 指令

本仓库规则以 `AGENTS.md` 为准。首要硬规则：

**仓库根目录 `README.md` 永久留空（0 字节）。禁止写入或改动任何内容。**

仅限这一个文件：`stock_*/README.md`、`web/README.md` 是正常文档，可照常更新，不要把规则套过去。

- 不要生成/补全根 `README.md` 的内容（标题、简介、安装、目录、badge 都不要）。
- 它是刻意留空，不是遗漏，不要"修复"。
- 文档写到 `INFORMATION.md`、`USAGE.md`，或对应模块目录下的 `README.md`。
- `.githooks/pre-commit` 会拒绝包含根 `README.md` 改动的提交。
