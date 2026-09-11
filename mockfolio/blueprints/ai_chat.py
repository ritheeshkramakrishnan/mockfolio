"""AI tutor chat, with a rule-based fallback when no AI key is configured."""
from flask import Blueprint, jsonify, request

from mockfolio import ai_client
from mockfolio.blueprints.auth import login_required

bp = Blueprint("ai_chat", __name__)


@bp.route("/api/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    data = request.get_json()
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    system_prompt = """You are QuantBot, an expert AI trading tutor for a paper trading platform.
You teach users about:
- Technical analysis patterns (head & shoulders, flags, wedges, RSI, MACD, Bollinger Bands, etc.)
- Quantitative methods: Monte Carlo simulation, Sharpe ratio, Value at Risk (VaR), Kelly criterion, mean reversion, momentum strategies
- Risk management: position sizing, stop-losses, drawdown, diversification
- Market microstructure: bid-ask spread, order flow, liquidity
- Statistical concepts: standard deviation, correlation, beta, alpha, regression

Keep responses concise (under 300 words), use examples, and be encouraging.
When explaining formulas, use plain text notation. Always connect theory to practical trading decisions."""

    if ai_client._ai_client:
        try:
            response = ai_client._ai_client.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}]
            )
            reply = response.content[0].text
        except Exception:
            reply = _fallback_ai(user_msg)
    else:
        reply = _fallback_ai(user_msg)

    return jsonify({"reply": reply})


def _fallback_ai(msg: str) -> str:
    msg_l = msg.lower()
    if "monte carlo" in msg_l:
        return ("Monte Carlo simulation runs thousands of random price paths using your asset's historical mean return (μ) "
                "and volatility (σ). Each path follows: P(t+1) = P(t) × (1 + N(μ, σ)). "
                "The distribution of final prices shows you the probability of profit/loss over your holding period. "
                "On this platform you can run it live in the Quant Lab → Monte Carlo tab.")
    if "sharpe" in msg_l:
        return ("The Sharpe Ratio = (Return − Risk-Free Rate) / Std Dev of Returns. "
                "It measures return per unit of risk. A Sharpe above 1.0 is good; above 2.0 is excellent. "
                "In paper trading, track your daily P&L, compute its mean and standard deviation, and annualise: multiply mean × 252 and std × √252.")
    if "rsi" in msg_l:
        return ("RSI (Relative Strength Index) oscillates 0–100. "
                "RSI < 30 = oversold (potential buy), RSI > 70 = overbought (potential sell). "
                "It's calculated from average gains vs average losses over N periods (default 14). "
                "Tip: RSI divergence — price makes a new high but RSI doesn't — often precedes reversals.")
    if "macd" in msg_l:
        return ("MACD = 12-period EMA − 26-period EMA. The Signal Line is a 9-period EMA of MACD. "
                "Buy signal: MACD crosses above signal. Sell signal: MACD crosses below. "
                "The histogram shows the gap between MACD and signal — widening histogram = strengthening trend.")
    if "kelly" in msg_l:
        return ("Kelly Criterion: f* = (bp − q) / b, where b = odds, p = win probability, q = 1−p. "
                "For trading: f* = (Win Rate / Loss Rate) − (Avg Loss / Avg Win). "
                "Most traders use half-Kelly (f*/2) to reduce variance. It tells you what fraction of capital to risk per trade.")
    if "var" in msg_l or "value at risk" in msg_l:
        return ("Value at Risk (VaR) estimates the maximum loss at a confidence level (e.g. 95%) over a time horizon. "
                "Parametric VaR = Portfolio Value × σ × Z-score. For 95% confidence, Z=1.645. "
                "Example: $50k portfolio, daily σ=1.5% → 95% VaR = $50,000 × 0.015 × 1.645 = $1,234 max daily loss.")
    return ("Great question! As your QuantBot tutor, I can explain trading patterns, quant methods (Monte Carlo, Sharpe, VaR, Kelly), "
            "risk management, and more. Try asking about: RSI, MACD, Bollinger Bands, Monte Carlo, Sharpe Ratio, Kelly Criterion, or Value at Risk. "
            "💡 Tip: Add your Anthropic API key in the .env file to unlock full AI responses.")
