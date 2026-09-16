# 待办：代理 Fake-IP 环境下的 SSRF Guard 兼容

> **状态：未开始实现。** 记录于 2026-09-16，要求在 Data Contract 收尾、CI 全绿之后再处理。
>
> 届时的 commit 建议：`fix(net): support proxy fake-ip without weakening ssrf guard`

## 问题

正常公网域名在 **Clash / Mihomo Fake-IP 环境**下解析为 `198.18.x.x`：

```text
news.china.com  →  198.18.1.127      （本机实测）
example.com     →  198.18.0.120      （本机实测，非单站）
```

当前 `net_guard` 按非公网地址**正确拒绝**——但这是代理环境下的正常公网网站，
属于**系统性误伤**，不是个别站点问题。

## 定性：不是安全缺陷，是环境兼容问题

Fake-IP 是代理对 DNS 的正常劫持方式：真实连接由代理发起，`198.18/15` 只是
代理的内部路由标记，**并不是真正连往 198.18.x.x**。

## 设计原则（实现时必须全部满足）

1. **不允许**通过简单开启 `allow_private_network` 解决。
2. `127/8`、RFC1918、link-local、metadata、**真实 `198.18/15` IP literal** 等仍必须默认拒绝。
3. 必须区分两种情况，不能视为同一种：
   * 用户直接输入 `http://198.18.1.127`（IP literal → 仍拒绝）
   * 公网 hostname 被系统 DNS 映射为 `198.18.x.x`（→ 走兼容路径）
4. hostname 解析结果落入 `198.18.0.0/15` 时，识别为「**疑似 Fake-IP 环境**」，
   不要直接当普通私网目标处理。
5. 设计 **proxy-aware / fake-ip-aware** 验证路径，保证最终仍有 SSRF 安全边界。
6. redirect 每跳仍必须重新校验（现有机制不得削弱）。
7. **禁止**因为 Fake-IP 兼容而放过：
   `localhost` / `127.0.0.1` / `[::1]` / RFC1918 / link-local / metadata IP /
   非 http/https scheme。

## 实现前必须先审计

**不要凭假设修改。** 先审计当前 `safe_fetch` 的 proxy/TUN 行为：

* `proxy_active()` 在 TUN 模式下是否为 True（TUN 可能不设置环境变量代理）
* TUN 模式下「连接到解析出的 IP」是否实际会把流量交给代理
* 钉住 IP（pinned connection）在 Fake-IP 环境下是否仍然成立：
  解析到 `198.18.x.x` 后直接连它，实际是发给代理的 TUN 网卡，由代理还原真实目标 ——
  这**恰好保持了 SSRF 边界**（连不到 127.0.0.1），但需要实测确认

## 必测回归（至少）

| 场景 | 预期 |
| :--- | :--- |
| 普通域名 → fake `198.18.x.x` | 应允许走兼容路径 |
| 用户直接输入 `198.18.x.x` | 仍拒绝 |
| fake-ip hostname → redirect localhost | 仍拒绝 |
| fake-ip hostname → redirect 私网 | 仍拒绝 |
| 无代理普通公网解析 | 原行为不变 |

## 边界提醒

本条只处理 **Fake-IP 兼容**。不允许顺带放宽其它非公网地址策略，
也不允许把 `allow_private_network` 的语义与 Fake-IP 兼容混在一起。
