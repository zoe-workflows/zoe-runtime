# zoe-runtime — 本地开发与发布入口
#
# 这些目标镜像 .github/workflows/*.yml 的步骤，本地能跑通的目标在 CI 里同名存在。
# `make` 默认打印帮助；没有参数时不会改动任何文件。
#
# Windows：GNU Make 不是标准组件，请在 WSL / Git Bash 下使用，或按 `make help`
# 里每个目标对应的命令直接执行（每条 recipe 都是单行 shell，可直接复制）。

SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV      ?= .venv
SKILL_DIR ?= ../zoe-workflows

# 装了 .venv 就用它，否则用环境里的 python3：CI 与已激活的 shell 都走后者。
PYTHON ?= $(shell if [ -x "$(VENV)/bin/python" ]; then echo "$(VENV)/bin/python"; \
                  elif [ -x "$(VENV)/Scripts/python.exe" ]; then echo "$(VENV)/Scripts/python.exe"; \
                  else command -v python3 || command -v python; fi)
# 需要在别的目录里执行时用绝对路径（recipe 会 cd 到临时目录）。
PYTHON_ABS := $(abspath $(PYTHON))

.PHONY: help setup install test contract lint fmt check verify build sdist-check \
        verify-dist release-check install-cli uninstall-cli demo version clean distclean

help: ## 显示所有目标
	@printf 'zoe-runtime — 可用目标（当前解释器：%s）\n\n' '$(PYTHON)'
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\n跨仓契约默认读取 %s（可用 SKILL_DIR=... 覆盖）。\n' '$(SKILL_DIR)'

setup: ## 创建 .venv 并安装开发依赖
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/python -m pip install -e '.[dev,build]'
	@echo "完成：source $(VENV)/bin/activate（$(VENV) 会被后续 make 自动使用）"

install: ## 在“当前解释器”安装（CI / 已激活环境），不改 .venv
	$(PYTHON) -m pip install -e '.[dev,build]'

test: ## 全量测试；缺少 skill 检出时跨仓用例自动跳过
	$(PYTHON) -m pytest -q

contract: ## 只跑跨仓契约与文档门禁（需要 skill 检出）
	@test -f "$(SKILL_DIR)/SKILL.md" || { echo "未找到 skill：SKILL_DIR=$(SKILL_DIR)，用 SKILL_DIR=/abs/path 指定"; exit 1; }
	ZOE_SKILL_DIR=$(abspath $(SKILL_DIR)) $(PYTHON) -m pytest -q \
	  tests/contract tests/test_doc_contracts.py tests/test_contract_artifacts.py

lint: ## ruff check（不含格式）
	$(PYTHON) -m ruff check .

fmt: ## 自动修复 + ruff format（会重排约 24 个文件，请单独提交）
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format .

check: lint test ## 提交前最小门禁：lint + 全量测试

build: ## 构建 sdist + wheel 并校验元数据
	rm -rf dist build
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*

sdist-check: build ## 从 sdist 重建 wheel（验证打包自洽，CI 步骤 5）
	@set -euo pipefail; tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	  tar -xzf dist/*.tar.gz -C "$$tmp"; \
	  src=$$(find "$$tmp" -maxdepth 1 -type d -name 'zoe_runtime-*' | head -n1); \
	  echo "从 $$(basename "$$src") 重建 wheel"; \
	  cd "$$src" && $(PYTHON_ABS) -m build --wheel --outdir "$$tmp/wheel"; \
	  ls -l "$$tmp/wheel"

verify-dist: build ## 在临时 venv 安装 wheel，跑发行验证（版本/兼容门禁/demo 护栏）
	@set -euo pipefail; tmp=$$(mktemp -d); trap 'rm -rf "$$tmp"' EXIT; \
	  $(PYTHON_ABS) -m venv "$$tmp/venv"; \
	  py=$$(ls "$$tmp/venv/bin/python" 2>/dev/null || echo "$$tmp/venv/Scripts/python.exe"); \
	  "$$py" -m pip -q install --upgrade pip; \
	  "$$py" -m pip -q install dist/*.whl; \
	  ( cd "$$tmp" && "$$py" -m src --version && "$$py" -m src capabilities --json >/dev/null ); \
	  ZOE_DIST_PYTHON="$$py" $(PYTHON_ABS) -m pytest -q tests/test_distribution.py

release-check: check sdist-check verify-dist ## 发布前全量：测试 + 打包 + 产物验证

install-cli: build ## 用 pipx 安装刚构建的 wheel（覆盖同版本已装）
	@command -v pipx >/dev/null || { echo "需要 pipx：brew install pipx / python3 -m pip install --user pipx"; exit 1; }
	@$(PYTHON) -c "import build" 2>/dev/null || { \
	  echo "当前解释器缺 build 模块：make install（装 .[dev,build]），或 $(PYTHON) -m pip install 'build>=1'"; exit 1; }
	pipx install --force dist/zoe_runtime-*.whl

uninstall-cli: ## 卸载 pipx 里的 CLI（保留任务状态与证据）
	@command -v pipx >/dev/null || { echo "需要 pipx"; exit 1; }
	pipx uninstall zoe-runtime

demo: ## 不安装、直接用当前树跑一次 demo（仿真，不调用真实 Agent）
	$(PYTHON) -m src demo --json

version: ## 打印包版本
	@$(PYTHON) -c "import src; print(src.__version__)"

clean: ## 清理构建产物与缓存（保留 .venv）
	rm -rf dist build
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

distclean: clean ## 清理并删除 .venv
	rm -rf $(VENV)
