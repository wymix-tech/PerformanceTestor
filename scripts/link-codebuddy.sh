#!/usr/bin/env bash
# =====================================================================
# CodeBuddy 用户级目录软链接脚本（Docker 容器启动时执行）
# 背景：容器每次启动都是全新环境，用户级 ~/.codebuddy 是临时目录；
#       真实数据持久化存储在工作区 /workspace/.codebuddy。
# 作用：1) 删除用户级 ~/.codebuddy（无论目录还是旧软链接）
#       2) 创建软链接 ~/.codebuddy -> /workspace/.codebuddy
# 用法：作为 Docker ENTRYPOINT 或在容器启动入口最先调用
#       （必须早于 CodeBuddy 进程启动，否则进程会重建物理目录）
# =====================================================================
set -e

USER_CODEBUDDY="${HOME:-/root}/.codebuddy"
WORKSPACE_CODEBUDDY="${WORKSPACE_CODEBUDDY:-/workspace/.codebuddy}"

# ---------- 安全检查：防止误删根目录 / HOME ----------
if [ -z "${USER_CODEBUDDY}" ] \
   || [ "${USER_CODEBUDDY}" = "/" ] \
   || [ "${USER_CODEBUDDY}" = "${HOME:-/root}" ]; then
  echo "[link-codebuddy] 错误：目标路径非法，已退出" >&2
  exit 1
fi

# ---------- 1. 确保工作区数据目录存在（真实目录） ----------
if [ ! -e "${WORKSPACE_CODEBUDDY}" ]; then
  mkdir -p "${WORKSPACE_CODEBUDDY}"
  echo "[link-codebuddy] 工作区目录不存在，已创建: ${WORKSPACE_CODEBUDDY}"
elif [ -L "${WORKSPACE_CODEBUDDY}" ]; then
  echo "[link-codebuddy] 错误：工作区 .codebuddy 是软链接（指向 $(readlink "${WORKSPACE_CODEBUDDY}")），拒绝嵌套链接" >&2
  exit 1
fi

# ---------- 2. 删除用户级 .codebuddy（目录或旧软链接） ----------
if [ -e "${USER_CODEBUDDY}" ] || [ -L "${USER_CODEBUDDY}" ]; then
  rm -rf "${USER_CODEBUDDY}"
  echo "[link-codebuddy] 已删除用户级: ${USER_CODEBUDDY}"
fi

# ---------- 3. 创建软链接 ----------
ln -s "${WORKSPACE_CODEBUDDY}" "${USER_CODEBUDDY}"
echo "[link-codebuddy] 软链接就绪: ${USER_CODEBUDDY} -> ${WORKSPACE_CODEBUDDY}"

# ---------- 透传主命令（作为 ENTRYPOINT 时使用） ----------
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
