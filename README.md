# AscendNPU-IR Doc Preview

为 [AscendNPU-IR](https://gitcode.com/Ascend/AscendNPU-IR) 提供 PR 文档预览服务。输入 PR 号或 commit SHA，自动拉取代码、构建 Sphinx 文档并在浏览器中预览。

## 功能

- 输入 **PR 号**（如 `123`）或 **commit SHA**（如 `abc1234`）自动识别
- 实时流式显示构建日志
- 构建完成后直接在页面内预览中英双语文档
- 按 commit SHA 缓存构建结果；PR 支持强制重新构建

## 快速开始

**1. 创建并激活 conda 环境**

```bash
conda create -n doc-prev python=3.11 -y
conda activate doc-prev
pip install -r requirements.txt
```

**2. 启动服务**

```bash
uvicorn app:app --reload --port 8080
```

**3. 打开浏览器**

访问 `http://localhost:8080`，输入 PR 号或 commit SHA，点击「构建」。

## 说明

- 首次构建时会自动将仓库 clone 到当前目录的 `AscendNPU-IR/` 下
- 构建结果缓存在 `builds/` 目录，按 job ID（`pr-{number}` 或 commit SHA）区分
- 构建使用 `make html-all`，同时生成英文（`en/`）和中文（`zh_cn/`）文档
