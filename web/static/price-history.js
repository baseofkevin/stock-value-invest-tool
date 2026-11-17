/* 单股多时点曲线：只读 #price-history-data 里的 JSON，不碰别的页面。
 *
 * 图上只画**未复权收盘价**；涨跌幅不单独画一条线，而是：
 *   - Tooltip 里给「当日 / 累计」两个百分比；
 *   - 点数不多时，在每个收盘价点上方标累计涨跌幅；点多时只标最后一个点。
 *
 * 横轴可能包含停牌日（close=null），Chart.js 的 spanGaps:false 会把线断开，
 * 不会把缺口画成一条连续的涨跌线。
 */
(function () {
  'use strict';

  const canvas = document.getElementById('price-history-chart');
  const dataEl = document.getElementById('price-history-data');
  if (!canvas || !dataEl || typeof window.Chart === 'undefined') return;

  let points;
  try {
    points = JSON.parse(dataEl.textContent || '[]');
  } catch (_err) {
    return;
  }
  if (!Array.isArray(points) || points.length < 2) return;

  const fmt = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }));
  const fmtPct = (v) => {
    if (v == null) return '—';
    const n = Number(v);
    return (Math.abs(n) < 0.005 ? '0.00' : (n > 0 ? '+' : '') + n.toFixed(2)) + '%';
  };
  const shortDate = (d) => String(d || '').slice(5);
  const pointRadius = points.length <= 3 ? 4 : 2;
  const lastPctIndex = points.reduce((acc, p, i) => (p.cum == null ? acc : i), -1);

  /* 在收盘价点上方标注累计涨跌幅。点多时只留最后一个，避免把图糊满。 */
  const pctLabels = {
    id: 'pctLabels',
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      const ctx = chart.ctx;
      const showAll = points.length <= 12;
      ctx.save();
      ctx.font = '600 10px ui-sans-serif, system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      meta.data.forEach((el, i) => {
        const p = points[i];
        if (!p || p.cum == null || el.skip) return;
        if (!showAll && i !== lastPctIndex) return;
        const x = Math.min(Math.max(el.x, 20), chart.width - 20);
        const y = Math.max(el.y - 9, 12);
        ctx.fillStyle = p.cum > 0 ? '#c62828' : (p.cum < 0 ? '#0e8a5f' : '#64748b');
        ctx.fillText(fmtPct(p.cum), x, y);
      });
      ctx.restore();
    },
  };

  new Chart(canvas, {
    type: 'line',
    plugins: [pctLabels],
    data: {
      labels: points.map((p) => shortDate(p.date)),
      datasets: [{
        label: '收盘价',
        data: points.map((p) => p.close),
        borderColor: '#0f766e',
        backgroundColor: 'rgba(15,118,110,.10)',
        borderWidth: 2,
        fill: false,
        tension: .24,
        pointRadius: pointRadius,
        pointHoverRadius: 5,
        pointBackgroundColor: '#0f766e',
        spanGaps: false,
      }],
    },
    options: {
      maintainAspectRatio: false,
      animation: false,
      interaction: {mode: 'index', intersect: false},
      layout: {padding: {top: 18, right: 10}},
      plugins: {
        legend: {display: false},
        tooltip: {
          callbacks: {
            title: (items) => (items[0] ? points[items[0].dataIndex].date : ''),
            label: (ctx) => {
              const p = points[ctx.dataIndex] || {};
              if (p.close == null) return '停牌 / 无数据';
              const parts = [`收盘 ${fmt(p.close)}`];
              if (p.pct_chg != null) parts.push(`当日 ${fmtPct(p.pct_chg)}`);
              if (p.cum != null) parts.push(`累计 ${fmtPct(p.cum)}`);
              return parts.join(' · ');
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {maxTicksLimit: 10, font: {size: 10}, color: '#64748b'},
          grid: {display: false},
        },
        y: {
          beginAtZero: false,
          ticks: {
            maxTicksLimit: 6,
            font: {size: 10},
            color: '#64748b',
            callback: (v) => Number(v).toFixed(2),
          },
          grid: {color: '#eef2f7'},
          title: {display: true, text: '收盘价（元）', color: '#64748b'},
        },
      },
    },
  });
})();
