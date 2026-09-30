# Kaggriculture Agent: Strategic Economic Planner

A rule-based AI agent for the **Kaggriculture** Kaggle competition, a multi-agent farming and economic simulation played over a 30-day season with limited resources and dynamic market prices.

The agent is deterministic and uses only the Python standard library, so it can be submitted as a single `main.py`.

**Best score to date:** 199.3 (competition ongoing, ends September 2026)

## How it works

The agent combines a tactical executor with an economic decision engine that runs once per day.

| Component | What it does |
|---|---|
| **Economic Engine** | Scores every crop and animal by expected net profit per tile per day (revenue minus amortized cost). Only considers options that can pay back their investment within the remaining season. |
| **Wheat Reserve Manager** | Keeps enough wheat in the shed to feed all owned animals for a safety buffer, independent of selling logic. |
| **Land and Hiring Controller** | Buys land only when the farm is crowded and enough season is left to earn it back. Hires workers only while the marginal cost is low and there is a real task backlog. |
| **Fertilizer ROI Trigger** | Fertilizes only premium crops (melon, strawberry), where the flat fertilizer cost actually pays off. |
| **End-Game Liquidation** | Stops long-horizon investments late in the season and force-sells all inventory on the final days, since unsold stock does not count toward the score. |
| **Worker Routing** | A from-scratch implementation of the **Hungarian (Kuhn-Munkres) assignment algorithm** that routes workers to tasks optimally under resource-compatibility constraints. |
| **Market and Opponent Model** | Tracks price history (trend, momentum, volatility), town demand signals, and opponent behavior. Land-expansion strategy was refined using competitor replay analysis. |
| **Forward Simulator** | A lightweight beam-search simulator used to compare capital allocation choices over a short horizon. |
| **Fail-Safe Layer** | Falls back to a proven baseline policy if any error occurs during execution. |

## Repository structure

```
.
├── kaggriculture_agent.py   # The full agent
└── README.md
```

## Usage

The agent exposes the standard Kaggle agent entry point. To submit, rename the file to `main.py` and upload it to the competition.

## Tech

- Python 3 (standard library only, no external dependencies)
- Algorithms: Hungarian assignment, beam search, rule-based scoring

## Author

**Amar Sayed Mohammed**
[LinkedIn](https://www.linkedin.com/in/amar-sayed-344549321) · [GitHub](https://github.com/Amar-Sayed) · [Hugging Face](https://huggingface.co/Amar524)
