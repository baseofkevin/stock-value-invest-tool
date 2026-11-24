"""HTML 报告样式。"""

HTML_CSS = """
:root{
  --bg:#f3f6fb; --panel:#ffffff; --ink:#162033; --muted:#6b778c;
  --line:#e6ebf3; --navy:#0b1b30; --teal:#0f766e;
  --warn:#b45309; --warn-bg:#fff7e6; --warn-line:#f59e0b;
  --good:#047857; --good-bg:#ecfdf5; --good-line:#10b981;
  --info:#1d4ed8; --info-bg:#eff6ff; --info-line:#60a5fa;
  --ok:#0f766e; --ok-bg:#f0fdfa; --ok-line:#14b8a6;
  --muted-bg:#f1f5f9; --muted-line:#94a3b8;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:
    radial-gradient(1100px 500px at 10% -10%,#e0f2fe 0,transparent 55%),
    radial-gradient(900px 500px at 100% 0,#ccfbf1 0,transparent 50%),
    var(--bg);
  color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.6;
}
.page{max-width:1180px;margin:0 auto;padding:28px 20px 44px}
.hero{
  position:relative;overflow:hidden;border-radius:26px;padding:30px 32px 26px;
  background:linear-gradient(130deg,#081524 0%,#0d2b3f 52%,#0f4c52 100%);
  color:#f8fbff;box-shadow:0 24px 60px rgba(8,21,36,.25);
}
.hero:after{
  content:"";position:absolute;right:-120px;top:-150px;width:420px;height:420px;border-radius:50%;
  background:radial-gradient(circle,rgba(45,212,191,.22),transparent 65%);
}
.eyebrow{font-size:11px;letter-spacing:.18em;color:#8ee7dc;font-weight:700;text-transform:uppercase}
.title-row{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:8px 0 6px;position:relative;z-index:1}
h1{font-size:31px;line-height:1.25;margin:0;letter-spacing:.01em}
.code{font-size:13px;font-weight:600;color:#b9d7e6;background:rgba(255,255,255,.1);
  border:1px solid rgba(255,255,255,.15);padding:3px 9px;border-radius:999px;margin-left:10px;vertical-align:middle}
.meta{font-size:13px;color:#b9c9d8;position:relative;z-index:1}
.meta strong{color:#fff;font-weight:600}
.notice{display:inline-flex;align-items:center;gap:8px;margin-top:14px;padding:7px 12px;border-radius:999px;
  background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.16);color:#d9fff8;font-size:12px;
  position:relative;z-index:1}
.dot{width:7px;height:7px;border-radius:50%;background:#2dd4bf;box-shadow:0 0 0 4px rgba(45,212,191,.15)}
.hero-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:22px;position:relative;z-index:1}
.hero-stat{background:rgba(255,255,255,.075);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:13px 14px}
.hero-stat label{display:block;font-size:11px;color:#a9c2d2;letter-spacing:.04em}
.hero-stat b{display:block;font-size:21px;color:#fff;margin:3px 0 1px;line-height:1.3}
.hero-stat small{display:block;font-size:11px;color:#a9c2d2}
.hero-stat b.neg{color:#7dd3c7}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:22px 24px;margin-top:18px;
  box-shadow:0 12px 32px rgba(15,23,42,.055)}
.section-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:16px}
h2{font-size:19px;line-height:1.3;margin:0 0 4px}
.section-head p{margin:0;font-size:12.5px;color:var(--muted)}
.factor-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}
.factor-card{
  border:1px solid var(--line);border-left:5px solid var(--line);border-radius:16px;padding:17px;
  background:linear-gradient(180deg,#fff,#fbfcff);display:flex;flex-direction:column;gap:12px;
  min-width:0;
}
.factor-card.wide{grid-column:1/-1}
.factor-card.tone-warn{border-left-color:var(--warn-line)}
.factor-card.tone-good{border-left-color:var(--good-line)}
.factor-card.tone-info{border-left-color:var(--info-line)}
.factor-card.tone-ok{border-left-color:var(--ok-line)}
.factor-card.tone-muted{border-left-color:var(--muted-line)}
.factor-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.factor-head h3{font-size:17px;line-height:1.3;margin:0}
.factor-head h3 small{display:block;font-size:11px;font-weight:500;color:var(--muted);margin-top:3px}
.state{display:inline-flex;align-items:center;white-space:nowrap;border-radius:999px;padding:4px 10px;
  font-size:12px;font-weight:700;border:1px solid transparent}
.state-warn{color:var(--warn);background:var(--warn-bg);border-color:#fde68a}
.state-good{color:var(--good);background:var(--good-bg);border-color:#a7f3d0}
.state-info{color:var(--info);background:var(--info-bg);border-color:#bfdbfe}
.state-ok{color:var(--ok);background:var(--ok-bg);border-color:#99f6e4}
.state-muted{color:#64748b;background:var(--muted-bg);border-color:#e2e8f0}
.headline{font-size:14px;font-weight:650;color:#243044;margin:0;line-height:1.55}
.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.metric{background:#f7f9fd;border:1px solid #edf1f7;border-radius:11px;padding:8px 10px;min-width:0}
.metric label{display:block;font-size:10.5px;color:var(--muted);line-height:1.4}
.metric b{display:block;font-size:14px;line-height:1.45;word-break:break-word}
.calc{background:#f8fafc;border:1px solid #e8eef6;border-radius:12px;padding:10px 12px}
.calc-title{font-size:11px;font-weight:700;letter-spacing:.06em;color:#64748b;margin-bottom:2px}
.calc-grid{display:grid;grid-template-columns:1fr;gap:0}
.factor-card.wide .calc-grid{grid-template-columns:1fr 1fr;column-gap:26px}
.calc-item{display:flex;justify-content:space-between;gap:14px;padding:5px 0;border-bottom:1px dashed #e2e8f0;
  font-size:11.5px;min-width:0}
.calc-item:last-child{border-bottom:0}
.calc-item span{color:#64748b;flex:0 0 auto}
.calc-item b{color:#1e293b;font-weight:600;text-align:right;word-break:break-word}
.earn-blocks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.earn-block{border:1px solid #e8eef6;border-top:4px solid #94a3b8;border-radius:14px;background:#fbfcff;padding:12px;min-width:0}
.earn-block.tone-warn{border-top-color:var(--warn-line)}
.earn-block.tone-good{border-top-color:var(--good-line)}
.earn-block.tone-info{border-top-color:var(--info-line)}
.earn-block h4{font-size:15px;margin:0;line-height:1.3}
.earn-block .earn-sub{font-size:10.5px;color:var(--muted);margin:2px 0 8px}
.earn-results{display:grid;gap:5px;margin-bottom:10px}
.earn-result{display:flex;justify-content:space-between;gap:10px;background:#fff;border:1px solid #edf1f7;border-radius:9px;padding:6px 8px;font-size:12px}
.earn-result span{color:#64748b;flex:0 0 auto}
.earn-result b{text-align:right;word-break:break-word}
.earn-title{font-size:10px;font-weight:700;letter-spacing:.06em;color:#64748b;margin:8px 0 4px}
.earn-source-row{display:flex;justify-content:space-between;gap:10px;font-size:11px;padding:3px 0;border-bottom:1px dashed #e2e8f0}
.earn-source-row:last-child{border-bottom:0}
.earn-source-row span{color:#64748b}
.earn-source-row b{text-align:right;font-weight:600;word-break:break-word}
.earn-formula{font-size:10.5px;color:#475569;background:#f1f5f9;border-radius:7px;padding:5px 7px;margin-top:4px;line-height:1.55;word-break:break-word}
.note{font-size:11.5px;color:#64748b;margin:0;line-height:1.65}
.warnings{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.warnings li{background:#fffaf0;border:1px solid #fde9c8;border-radius:10px;padding:9px 11px;font-size:12px;color:#7c4a03}
.empty{background:#f8fafc;border:1px dashed #dbe4f0;border-radius:10px;padding:10px 12px;color:#64748b;font-size:12px}
.foot{margin-top:20px;padding:2px 4px;color:#7c8798;font-size:11.5px;line-height:1.8}
.foot strong{color:#475569}
@media (max-width:960px){
  .factor-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .factor-card.wide{grid-column:1/-1}
  .earn-blocks{grid-template-columns:1fr}
  .hero-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
/* 旁证表 + 缺口清单表：都是"读一行就走"的信息，不做卡片化，保持表格的扫描速度 */
table.evidence,table.gaps,table.kv{width:100%;border-collapse:collapse;font-size:12.5px}
table.evidence th,table.gaps th,table.kv th{text-align:left;vertical-align:top;
  padding:8px 10px;color:#334155;font-weight:600;white-space:nowrap;
  border-bottom:1px solid #eef2f7;width:1%}
table.evidence td,table.gaps td,table.kv td{padding:8px 10px;color:#475569;
  border-bottom:1px solid #eef2f7;line-height:1.55}
table.evidence tr:last-child th,table.evidence tr:last-child td,
table.gaps tr:last-child td,table.kv tr:last-child th,table.kv tr:last-child td{border-bottom:0}
table.gaps thead th{background:#f8fafc;border-bottom:1px solid #e2e8f0;
  font-size:11.5px;color:#64748b;letter-spacing:.02em}
table.gaps td.ok{color:#15803d}
/* 表格行也认 tone：warn 提醒、good 通过、muted 缺数据，与卡片同一套语言 */
table.evidence tr.tone-warn td,table.gaps tr.tone-warn td{color:#9a3412}
table.evidence tr.tone-good td{color:#166534}
table.evidence tr.tone-muted td{color:#94a3b8}
table.evidence tr.tone-warn th{color:#c2410c}
table.evidence tr.tone-muted th{color:#94a3b8}
/* 旁证表第三列：一句话解读。浅底色块让它和中间的数值列一眼区分开 */
table.evidence td.read{width:34%;color:#64748b;background:#f8fafc;
  border-left:1px solid #eef2f7;font-size:12px}
@media (max-width:640px){
  table.evidence th,table.gaps th,table.kv th{white-space:normal}
  table.evidence th,table.evidence td,table.gaps th,table.gaps td,
  table.kv th,table.kv td{padding:7px 6px;font-size:12px}
  .page{padding:14px 11px 28px}
  .hero{padding:22px 18px;border-radius:20px}
  h1{font-size:24px}
  .code{margin-left:6px}
  .hero-grid{grid-template-columns:1fr 1fr;gap:9px}
  .panel{padding:17px 15px;border-radius:16px}
  .factor-grid{grid-template-columns:1fr}
  .factor-card.wide{grid-column:auto}
  .factor-card.wide .calc-grid{grid-template-columns:1fr}
  .earn-blocks{grid-template-columns:1fr}
}
@media print{
  body{background:#fff}
  .page{max-width:none;padding:0}
  .hero{box-shadow:none;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .panel{box-shadow:none;break-inside:avoid}
  .factor-card{break-inside:avoid}
}
"""
