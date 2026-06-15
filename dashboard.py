import threading
from flask import Flask, render_template_string, jsonify
from config import Config
import logging
from datetime import datetime

logger = logging.getLogger("Dashboard")

DASHBOARD_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JM Trading Bot V3.4</title>
<style>
body{font-family:system-ui;background:#1a1a2e;color:#eee;margin:0;padding:20px}
.card{background:#16213e;border-radius:12px;padding:15px;margin:10px 0}
h1{color:#00d4ff}h2{color:#7b68ee}.green{color:#00ff88}.red{color:#ff4757}.yellow{color:#ffa502}
table{width:100%;border-collapse:collapse}th,td{padding:8px;text-align:left;border-bottom:1px solid #333}th{color:#00d4ff}
canvas{width:100%;max-height:200px;margin:10px 0}
.badge{padding:4px 8px;border-radius:6px;font-size:.8em;font-weight:bold}
.badge-up{background:#00ff88;color:#000}.badge-down{background:#ff4757;color:#fff}.badge-turbo{background:#ffa502;color:#000}.badge-scalp{background:#ff6b6b;color:#fff}
</style></head><body>
<h1>JM Trading Bot V3.4</h1>
<div class="card"><h2>Resume</h2>
<p>Mode: <span id="mode">-</span> | Capital: <span id="capital">-</span> USDC</p>
<p>P&L jour: <span id="pnl_day">-</span> | P&L cumule: <span id="pnl_cumul">-</span> USDC</p></div>
<div class="card"><h2>Graphique P&L</h2><canvas id="pnlChart"></canvas></div>
<div class="card"><h2>Positions (<span id="pos_count">0</span>)</h2>
<table><thead><tr><th>Paire</th><th>Entree</th><th>Actuel</th><th>P&L</th><th>Stop</th></tr></thead><tbody id="pos_body"></tbody></table></div>
<div class="card"><h2>Derniers trades</h2>
<table><thead><tr><th>Paire</th><th>P&L USDC</th><th>P&L %</th><th>Raison</th><th>Date</th></tr></thead><tbody id="trades_body"></tbody></table></div>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
let chart;
function drawChart(data){
    const ctx=document.getElementById('pnlChart').getContext('2d');
    if(chart) chart.destroy();
    chart=new Chart(ctx,{type:'bar',data:{labels:data.map(d=>d.symbol),datasets:[{label:'P&L USDC',data:data.map(d=>d.pnl_usdc),backgroundColor:data.map(d=>d.pnl_usdc>=0?'#00ff88':'#ff4757')}]},options:{plugins:{legend:{display:false}},scales:{y:{beginAtZero:true}}}});
}
function refresh(){
    fetch('/api/status').then(r=>r.json()).then(d=>{
        let modeHtml = d.mode;
        if(d.turbo) modeHtml += ' <span class="badge badge-turbo">TURBO</span>';
        if(d.scalp) modeHtml += ' <span class="badge badge-scalp">SCALPING</span>';
        document.getElementById('mode').innerHTML = modeHtml;
        document.getElementById('capital').textContent=d.capital;
        document.getElementById('pnl_day').textContent=d.pnl_day+' USDC';
        document.getElementById('pnl_cumul').textContent=d.pnl_cumul;
        document.getElementById('pos_count').textContent=d.positions.length;
        let ph='';d.positions.forEach(p=>{ph+=`<tr><td>${p.symbol}</td><td>${p.entry}</td><td>${p.current}</td><td class="${p.pnl>0?'green':'red'}">${p.pnl>0?'+':''}${p.pnl}%</td><td>${p.stop}</td></tr>`});
        document.getElementById('pos_body').innerHTML=ph;
        let th='';d.trades.forEach(t=>{th+=`<tr><td>${t.symbol}</td><td class="${t.pnl_usdc>0?'green':'red'}">${t.pnl_usdc>0?'+':''}${t.pnl_usdc}</td><td class="${t.pnl>0?'green':'red'}">${t.pnl>0?'+':''}${t.pnl}%</td><td>${t.reason}</td><td>${t.time}</td></tr>`});
        document.getElementById('trades_body').innerHTML=th;
        if(d.trades.length>0) drawChart(d.trades.reverse());
    });
}
setInterval(refresh,10000);refresh();
</script>
</body></html>"""

class Dashboard:
    def __init__(self, db, binance_client):
        self.db = db
        self.binance = binance_client
        self.app = Flask(__name__)

        @self.app.route('/')
        def index():
            return render_template_string(DASHBOARD_TEMPLATE)

        @self.app.route('/api/status')
        def api_status():
            positions = self.db.get_positions()
            pos_data = []
            for p in positions:
                cp = self.binance.get_current_price(p['symbol'])
                if cp:  # prix depuis cache BinanceClientWrapper (TTL=3s)
                    pnl = (float(cp) - float(p['entry_price'])) / float(p['entry_price']) * 100
                    pos_data.append({
                        'symbol': p['symbol'], 'entry': f"{float(p['entry_price']):.4f}",
                        'current': f"{cp:.4f}", 'pnl': round(pnl, 2),
                        'stop': f"{float(p['current_stop']):.4f}"
                    })

            trades = self.db.get_trades(10)
            trade_data = [{
                'symbol': t['symbol'], 'pnl': round(float(t['pnl_pct']), 2),
                'pnl_usdc': round(float(t['pnl_usdc']), 2),
                'reason': t['exit_reason'], 'time': t['timestamp'][:16]
            } for t in trades]

            cumulative = self.db.get_cumulative_pnl()
            today = datetime.now().strftime('%Y-%m-%d')
            daily_trades = [t for t in self.db.get_trades(100) if t['timestamp'][:10] == today]
            today_pnl = sum(float(t['pnl_usdc']) for t in daily_trades)

            return jsonify({
                'mode': 'PROD' if not Config.DRY_RUN else 'DRY RUN',
                'turbo': Config.TURBO_MODE, 'scalp': Config.SCALP_MODE,
                'capital': round(self.binance.get_account_balance("USDC"), 2),
                'pnl_day': round(today_pnl, 2), 'pnl_cumul': round(cumulative, 2),
                'positions': pos_data, 'trades': trade_data
            })

    def run(self):
        threading.Thread(target=lambda: self.app.run(host='0.0.0.0', port=Config.DASHBOARD_PORT, debug=False, use_reloader=False), daemon=True).start()
        logger.info(f"Dashboard sur port {Config.DASHBOARD_PORT}")