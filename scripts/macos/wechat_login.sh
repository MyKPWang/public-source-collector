#!/bin/bash
# 微信公众平台扫码登录（重新获取有效 cookie/token）。
#
# 背景：微信后台接口的凭据会过期，失效后表现��� searchbiz 返回 ret=200003，
# 采集器无法读取公众号文章。本脚本走官方扫码登录流程重建凭据。
#
# 流程：
#   1. 启动 admin API（4101），使用 local-machine.env 中的管理员口令
#   2. 调用 /api/v1/admin/session 建立管理会话（拿到 CSRF token）
#   3. 调用 /api/v1/admin/login-sessions 创建扫码会话
#   4. 拉取二维码 PNG，输出路径供人工扫码
#   5. 轮询会话状态；成功后立刻用新凭据做一次真实文章检索做验证
#
# 用法： bash scripts/macos/wechat_login.sh
# 依赖： collector 的 .venv、ai-news-pipeline/deploy/macos/local-machine.env
set -uo pipefail

COLLECTOR_DIR="${COLLECTOR_DIR:-$HOME/Developer/public-source-collector}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$HOME/Developer/ai-news-pipeline/deploy/macos/local-machine.env}"
OUT_PNG="${OUT_PNG:-$HOME/.hermes/cache/scratch/wechat_login_qr.png}"
PORT="${PORT:-4101}"
BASE="http://127.0.0.1:${PORT}"
JAR="$(mktemp -t admin_cookies)"
CSRF_FILE="$(mktemp -t admin_csrf)"
trap 'rm -f "$JAR" "$CSRF_FILE"' EXIT

PY="$COLLECTOR_DIR/.venv/bin/python"
CLI="$COLLECTOR_DIR/.venv/bin/public-source-collector"
[ -x "$PY" ] || { echo "ERROR: 找不到 $PY"; exit 1; }
[ -x "$CLI" ] || { echo "ERROR: 找不到 $CLI"; exit 1; }
[ -f "$ENV_FILE" ] || { echo "ERROR: 找不到 $ENV_FILE"; exit 1; }

cd "$COLLECTOR_DIR" || exit 1
set -a; . "$ENV_FILE"; set +a

# 重要：admin 进程启动时会调用 expire_login_sessions_after_restart()，
# 把所有「活跃」扫码会话置为 expired。所以多个 admin 实例互相重启时，
# 会话会被反复误杀（DB 里表现为 PROCESS_RESTARTED / 授权进程已重启）。
# 因此：已有健康 admin 就直接复用，只有在没有时才启动；并且只清理自己启动的进程。
ADMIN_PID=""
ADMIN_REUSED=0
if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE/admin/login" 2>/dev/null)" = "200" ]; then
  echo "==> 复用已在运行的 admin API (${PORT})"
  ADMIN_REUSED=1
else
  echo "==> 启动 admin API (${PORT})"
  "$CLI" --config collector.toml admin --host 127.0.0.1 --port "$PORT" \
    >/tmp/wechat_login_admin.log 2>&1 &
  ADMIN_PID=$!
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$BASE/admin/login" 2>/dev/null)"
    [ "$code" = "200" ] && break
    sleep 1
  done
  if [ "$code" != "200" ]; then
    echo "ERROR: admin API 未就绪"; tail -20 /tmp/wechat_login_admin.log; kill "$ADMIN_PID" 2>/dev/null; exit 1
  fi
fi

echo "==> 登录管理控制台"
curl -s -D "$CSRF_FILE" -c "$JAR" -X POST -H 'Content-Type: application/json' \
  --data "{\"password\":\"${WECHAT_COLLECTOR_ADMIN_PASSWORD}\"}" \
  "$BASE/api/v1/admin/session" >/dev/null
CSRF="$(awk 'BEGIN{IGNORECASE=1} /^x-csrf-token:/{gsub(/\r/,""); print $2}' "$CSRF_FILE")"
[ -n "$CSRF" ] || { echo "ERROR: 未取得 CSRF token"; kill "$ADMIN_PID" 2>/dev/null; exit 1; }

echo "==> 创建扫码会话"
SID="$(curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST --data '{"created_by":"cli","channel":"local_web"}' \
  "$BASE/api/v1/admin/login-sessions" \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
[ -n "$SID" ] || { echo "ERROR: 创建扫码会话失败"; kill "$ADMIN_PID" 2>/dev/null; exit 1; }

echo "==> 拉取二维码"
mkdir -p "$(dirname "$OUT_PNG")"
# 二维码是 Playwright 渲染登录页后截图得到的，浏览器还在启动时会返回 QR_UNAVAILABLE，
# 因此必须轮询重试，不能只取一次。
QR_OK=0
for attempt in $(seq 1 30); do
  code="$(curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" \
    -o "$OUT_PNG" -w '%{http_code}' "$BASE/api/v1/admin/login-sessions/$SID/qr")"
  # 用 od 比较 PNG magic bytes；不能用 `head -c 4 | grep -q 'PNG'`：
  # 在 set -o pipefail 下 grep -q 提前关闭管道会触发 SIGPIPE，导致整条判断为假。
  magic="$(head -c 4 "$OUT_PNG" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  if [ "$code" = "200" ] && [ "$magic" = "89504e47" ]; then
    QR_OK=1
    break
  fi
  sleep 2
done
if [ "$QR_OK" != "1" ]; then
  echo "ERROR: 二维码获取失败（会话状态见下）"
  [ "$ADMIN_REUSED" = "1" ] || kill "$ADMIN_PID" 2>/dev/null
  exit 1
fi

# 放大只是提升可扫��性，失败也不该中断登录流程
"$PY" "$SCRIPT_DIR/_upscale_qr.py" "$OUT_PNG" >/dev/null 2>&1 || true
echo "二维码: $OUT_PNG ($(wc -c < "$OUT_PNG" | tr -d ' ') 字节)"

echo "==> 请用微信扫码（会话最长 5 分钟）"
while true; do
  BODY="$(curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" "$BASE/api/v1/admin/login-sessions/$SID")"
  ST="$(printf '%s' "$BODY" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("status",""))
except Exception: print("")')"
  MSG="$(printf '%s' "$BODY" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("message",""))
except Exception: print("")')"
  echo "    status=$ST $MSG"
  case "$ST" in
    succeeded) ;;
    failed|expired|cancelled)
      echo "==> 登录未完成 ($ST $MSG)"
      [ "$ADMIN_REUSED" = "1" ] || kill "$ADMIN_PID" 2>/dev/null
      exit 1 ;;
  esac
  [ "$ST" = "succeeded" ] && break
  sleep 4
done

echo "==> 登录成功，开始验证能否真的读到公众号文章"
cd "$COLLECTOR_DIR" || exit 1
"$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "src")
from pathlib import Path
from wechat_list_collector.bootstrap import create_production_runtime
from wechat_list_collector.config import load_settings
from wechat_list_collector.wechat_client import WeChatListClient

settings = load_settings(Path("collector.toml"))
rt = create_production_runtime(settings)
creds = rt.vault.load_wechat()
client = WeChatListClient(creds, timeout_seconds=30)
try:
    res = client.search("DeepSeek", limit=3)
except Exception as exc:
    print("SEARCH_ERR:", type(exc).__name__, str(exc)[:300])
    sys.exit(1)
print("SEARCH_OK count=%d" % len(res))
for item in res[:3]:
    print("   -", item.get("nickname"), "| fakeid:", str(item.get("fakeid"))[:24])
PYEOF
RC=$?
[ "$ADMIN_REUSED" = "1" ] || kill "$ADMIN_PID" 2>/dev/null
exit $RC
