#!/bin/sh
# 公网路由验收：在 206（或任意能访问公网的机器）执行。
# 用法：sh verify-routes.sh [base_url]
# 退出码 0 = 全部符合预期；非 0 = 有路由异常，按 SKILL.md 的故障对照表排查。

B=${1:-https://ai.boraydata.com}
fail=0

# 期望码说明：
#   401  = 落到 208 本机 8765（token 申请页 / 默认后端），说明该前缀没有专属 location
#   400  = 已到 MCP 服务（GET 无 session id 时 JSON-RPC 返回 400），说明反代链路通
#   200  = 静态资源 / health / 首页正常
check() {
    exp=$1
    path=$2
    # -4：本机 DNS 偶尔对 AAAA 解析超时（5s/10s 阶梯），会让断言假失败成 000
    code=$(curl -4 -s -o /dev/null -m 20 -w '%{http_code}' "$B$path")
    if [ "$code" = "$exp" ]; then
        printf 'OK   %-42s %s\n' "$path" "$code"
    else
        printf 'FAIL %-42s 期望 %s 实际 %s\n' "$path" "$exp" "$code"
        fail=1
    fi
}

echo "== 网关基础 =="
check 200 /
check 200 /token
check 401 /conring/

echo "== MCP 反代（nginx 不做 token 校验，400 表示已抵达 206 的 MCP 服务） =="
check 400 /conring/asr/mcp
check 400 /conring/tts/mcp
check 400 /conring/mail/ceo/mcp

echo "== 静态资源与调度 =="
check 200 /conring/asr/assets/avatars/ceo.png
check 200 /conring/dispatch/health

echo "== media-engine（9200，21 网段万兆） =="
check 200 /conring/media/health
check 200 /conring/media/v1/voices
# /files 无鉴权，必须在 nginx 层被拦成 404
check 404 /conring/media/files/any.wav
check 200 /conring/media/v1/assets/intro/cto.mp4

if [ "$fail" -eq 0 ]; then
    echo "全部通过"
else
    echo "存在失败项：检查 206 对应端口是否在 0.0.0.0 监听、208 上 nginx -t 是否通过"
fi
exit "$fail"
