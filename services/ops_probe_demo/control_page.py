"""故障注入控制台的网页版（探针控制口的 `GET /`）。**演示件。**

## 为什么这个页面由探针提供，而不是加进平台的管理后台

`CLAUDE.md` §5 记着这个项目踩过的一次 P0：一组标着【测试专用，正式上线前删除】
的管理端点能绕过 ACL 查/清任意企业的知识库，最终被整体删除，结论是
**「运行时开关不能替代上线前整体删除」**。

把"让被监控系统坏掉"的能力加进平台后端，是同一个形态：生产代码里多出一个
测试专用端点、平台还得去连探针的控制口。改由探针自己提供页面之后：
平台侧零改动、没有"上线前要记得删"的东西、它住在 `services/ops_probe_demo/`
里物理上进不了产品包，而且跟命令行共用同一个安全边界（只监听 127.0.0.1）。

## 页面本身

单文件、无外部依赖（内网/离线也能开）、深色配色跟运维塔台一致，
但**刻意不做得像产品界面**——顶部横幅明确写着这是演示工具，
免得截图流传出去被当成产品功能。
"""

from __future__ import annotations

from typing import Dict

from services.ops_probe_demo.environments import FAULT_KINDS, Environment


def render(env: Environment, faults: Dict[str, str], *,
           platform: str = "", connection_id: str = "", org_label: str = "") -> str:
    """⚠️ **必须显示"这个控制口接的是哪个平台、哪个连接器"。**

    第一版只显示环境名（"电商主站"），而三套环境的默认都是它——本机同时跑
    多个后端/多个探针时（开发常态），根本分辨不出眼前这个页面控制的是哪一个。
    实际就踩了：在 9330 上注入故障，却跑去另一个后端的界面上找变化，找不到。
    企业名同样要显示——连接器是挂在某个企业名下的，看的人得知道该用哪个企业的
    账号去登录才看得到变化。
    """
    services = sorted(env.services)
    kind_opts = "".join(
        f'<option value="{k}">{v.label}（{k} → {v.suggested_action}）</option>'
        for k, v in FAULT_KINDS.items()
    )
    rows = "".join(
        f"""<tr>
              <td class="svc">{s}</td>
              <td>{_status_cell(faults.get(s))}</td>
              <td class="act">
                <select id="kind-{s}">{kind_opts}</select>
                <button class="inject" onclick="inject('{s}')">注入故障</button>
                <button class="heal" onclick="heal('{s}')" {'' if s in faults else 'disabled'}>恢复</button>
              </td>
            </tr>"""
        for s in services
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>故障注入 · {env.label}</title>
<style>
  :root {{ color-scheme: dark; }}
  /* ⚠️ `html` 也要上色。只给 body 上色的话，body 覆盖不到的地方（窗口比
     body 宽的那一条、body 高度不足时的下方）会露出浏览器默认的白底——
     在一个整屏深色的页面上非常扎眼。运维塔台那边踩过同一个坑。 */
  html {{ background:#12131a; min-height:100%; }}
  body {{ margin:0; min-height:100vh; background:#12131a; color:#eef0f6;
          font-family:"Segoe UI",system-ui,sans-serif; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:24px 20px 48px; }}
  .banner {{ background:#3d3316; border:1px solid #fbbf24; color:#fbbf24;
             border-radius:8px; padding:10px 14px; font-size:13px; margin-bottom:20px; }}
  h1 {{ font-size:19px; margin:0 0 4px; }}
  .sub {{ color:#8d90a3; font-size:12.5px; font-family:ui-monospace,Menlo,monospace; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; background:#191b25;
           border:1px solid #2b2e3d; border-radius:10px; overflow:hidden; }}
  th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #22242f; font-size:13px; }}
  th {{ color:#8d90a3; font-weight:600; font-size:12px; }}
  tr:last-child td {{ border-bottom:none; }}
  .svc {{ font-family:ui-monospace,Menlo,monospace; }}
  .act {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
  select {{ background:#0d0e13; color:#eef0f6; border:1px solid #2b2e3d;
            border-radius:6px; padding:5px 8px; font-size:12.5px; }}
  button {{ border:none; border-radius:6px; padding:6px 12px; font-size:12.5px; cursor:pointer; }}
  button:disabled {{ opacity:.35; cursor:not-allowed; }}
  .inject {{ background:#8b7ffb; color:#0d0b1f; font-weight:600; }}
  .heal {{ background:#21232f; color:#eef0f6; border:1px solid #2b2e3d; }}
  .pill {{ display:inline-flex; align-items:center; gap:6px; font-size:11.5px;
           font-weight:600; padding:3px 9px; border-radius:20px; }}
  .ok {{ color:#34d399; background:#163a2d; }}
  .bad {{ color:#f8717a; background:#3d1e22; }}
  .bar {{ display:flex; gap:10px; margin-top:18px; }}
  .note {{ color:#5c5f70; font-size:12px; margin-top:20px; line-height:1.7; }}
  /* 接在哪个平台上，是这个页面最容易搞错也最要命的一条信息，给它足够的存在感。 */
  .target {{ background:#21232f; border:1px solid #2b2e3d; border-radius:8px;
             padding:9px 13px; font-size:12.5px; color:#8d90a3; margin-bottom:18px; }}
  .target b {{ color:#eef0f6; }}
  .target code {{ color:#8b7ffb; font-family:ui-monospace,Menlo,monospace; }}
  a {{ color:#8b7ffb; }}

  /* ⚠️ 窄屏下**不要让表格横向滚动**。这个页面的用途是"演示时随手点一下把某个
     服务弄坏"，横着拖找按钮完全违背它存在的意义。改成每个服务一块：
     服务名 + 状态一行，下拉和两个按钮各占一行。
     用区间写法（这个项目踩过"只写上界的规则被后面的覆盖"）。 */
  @media (max-width: 620px) {{
    .wrap {{ padding:16px 12px 32px; }}
    table, tbody, tr, td {{ display:block; width:100%; }}
    thead {{ display:none; }}          /* 表头在竖排下没有意义 */
    tr {{ border-bottom:1px solid #22242f; padding:10px 0; }}
    tr:last-child {{ border-bottom:none; }}
    td {{ border-bottom:none; padding:4px 12px; }}
    .svc {{ font-size:14px; font-weight:600; }}
    .act {{ flex-direction:column; align-items:stretch; gap:8px; }}
    .act select, .act button {{ width:100%; padding:9px 12px; font-size:13px; }}
    /* 手指点得到：44px 是触摸目标的通行下限，桌面上的 6px 内边距太小了。 */
    .bar button {{ width:100%; min-height:44px; }}
  }}
</style></head><body><div class="wrap">
  <div class="banner">⚠️ 这是<b>演示/测试工具</b>，不是产品功能。它能让被监控系统随时"坏掉"，
     只监听 127.0.0.1，不做鉴权，永远不会随产品发布。</div>
  <h1>故障注入 · {env.label}</h1>
  <div class="sub">环境 {env.key}　集群 {env.cluster}</div>
  <div class="target">
    <b>这个控制口接的是</b>　企业 <code>{org_label or "(未知)"}</code>
    　平台 <code>{platform or "(未知)"}</code>　连接器 <code>{connection_id or "(未知)"}</code>
  </div>
  <table>
    <!-- ⚠️ 表头必须显式包在 <thead> 里。不写的话浏览器会把这行 <tr> 塞进
         自动生成的 <tbody>，于是窄屏那条 `thead {{ display:none }}` 一点作用
         都没有——表头会当成一块内容竖着显示出来。真机渲染才看得出来。 -->
    <thead>
      <tr><th style="width:34%">服务</th><th style="width:22%">当前状态</th><th>操作</th></tr>
    </thead>
    <tbody>
    {rows}
    </tbody>
  </table>
  <div class="bar"><button class="heal" onclick="heal(null)">全部恢复</button></div>
  <div class="note">
    注入之后去<a href="/" onclick="return false;">运维塔台</a>看服务变色；
    跑一次「分析 → 提议 → 审批 → 执行」，<b>执行成功会自动恢复该服务</b>。<br>
    同样的操作也可以走命令行：<code>python -m services.ops_probe_demo.control inject
    --service &lt;服务&gt; --kind &lt;类型&gt;</code>
  </div>
</div>
<script>
  async function post(path, body) {{
    const r = await fetch(path, {{ method:'POST', headers:{{'Content-Type':'application/json'}},
                                  body: JSON.stringify(body) }});
    if (!r.ok) {{ alert('失败：' + await r.text()); return; }}
    // 直接整页刷新而不是局部更新——这是个几十行的演示页面，
    // 引入状态同步逻辑只会多一处可能跟真实状态不一致的地方。
    location.reload();
  }}
  function inject(s) {{ post('/inject', {{ service:s, kind: document.getElementById('kind-'+s).value }}); }}
  function heal(s) {{ post('/heal', s ? {{ service:s }} : {{}}); }}
</script></body></html>"""


def _status_cell(fault: str | None) -> str:
    if not fault:
        return '<span class="pill ok">● 正常</span>'
    return f'<span class="pill bad">● {FAULT_KINDS[fault].label}</span>'
