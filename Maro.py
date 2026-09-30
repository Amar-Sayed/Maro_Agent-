"""
Kaggriculture agent - Strategic Economic Planner

Single-agent architecture: state intelligence + economic allocator + market
forecasting + opponent model + farm/worker routing + expansion control +
endgame controller + lightweight forward simulator.

The planner is deliberately deterministic and standard-library only so it can
be submitted as a single main.py.

Kaggriculture agent - v2
Adds a real Economic Decision Engine on top of the v1 tactical executor:
  1. Economic Engine (once/day): scores every crop AND every animal by
     expected net profit per tile-per-day (revenue - amortized cost),
     gated by feasibility (enough days left to pay back the investment),
     nudged by essentiality/opponent-scarcity.
  2. Wheat Reserve Manager: keeps enough wheat in the shed to feed all
     owned animals for a safety buffer, independent of selling logic.
  3. Land + Hiring cost-benefit: only buys land when the farm is crowded
     and there's enough season left to earn it back; only hires while
     marginal hire cost stays cheap and there's a real task backlog.
  4. Fertilizer ROI trigger: only fertilizes premium crops (melon,
     strawberry) since fertilizer's flat $100 cost only pays off on
     high base-price crops.
  5. End-Game Liquidation: stops new long-horizon investment once there
     isn't enough season left to recoup it, and force-sells everything
     on the final days since unsold inventory doesn't count for score.
"""

# ---------------------------------------------------------------------------
# Static game data
# ---------------------------------------------------------------------------
CROPS = {
    "WHEAT":      {"seed": 10, "first_yield_day": 2, "max_yield_day": 4, "interval": 0, "max_yield": 6, "ongoing": False, "base_price": 25},
    "CARROT":     {"seed": 20, "first_yield_day": 2, "max_yield_day": 3, "interval": 0, "max_yield": 4, "ongoing": False, "base_price": 35},
    "TOMATO":     {"seed": 50, "first_yield_day": 8, "max_yield_day": 8, "interval": 1, "max_yield": 4, "ongoing": True, "base_price": 60},
    "STRAWBERRY": {"seed": 100, "first_yield_day": 10, "max_yield_day": 10, "interval": 2, "max_yield": 4, "ongoing": True, "base_price": 120},
    "MELON":      {"seed": 80, "first_yield_day": 10, "max_yield_day": 12, "interval": 0, "max_yield": 6, "ongoing": False, "base_price": 250},
}
YIELD_PER_TILE_DAY = {
    "WHEAT": 2.00, "CARROT": 2.00, "TOMATO": 4.00, "STRAWBERRY": 2.00, "MELON": 2.00,
}
ANIMALS = {
    "GOOSE": {"cost": 300, "structure": "COOP", "product": "EGG", "yield_per_day": 1.00, "first_yield_day": 4, "interval": 1, "max_held": 4},
    "COW": {"cost": 400, "structure": "PASTURE", "product": "MILK", "yield_per_day": 0.50, "first_yield_day": 8, "interval": 2, "max_held": 6},
    "SHEEP": {"cost": 500, "structure": "PASTURE", "product": "WOOL", "yield_per_day": 1/3, "first_yield_day": 6, "interval": 3, "max_held": 6},
}
PRODUCT_BASE_PRICE = {
    "WHEAT": 25, "CARROT": 35, "TOMATO": 60, "STRAWBERRY": 120, "MELON": 250,
    "EGG": 50, "MILK": 160, "WOOL": 200, "FERTILIZER": 100,
}
ESSENTIALITY = {
    "WHEAT": 10.0, "STRAWBERRY": 6.5, "CARROT": 6.0, "MILK": 5.0,
    "TOMATO": 3.5, "EGG": 3.0, "WOOL": 2.0, "MELON": 1.0,
}
PREMIUM_CROPS = {"MELON", "STRAWBERRY"}  # worth spending $100 on fertilizer

# Strategic controller constants. These are deliberately conservative: the
# planner may override a tactical rule only when the expected gain is clear.
BUILD_COST = {"COOP": 0, "PASTURE": 0}  # BUILD_* changes the tile; the official simulator charges no coins
MAX_FORWARD_DAYS = 6
MARKET_HISTORY_LEN = 12
RISK_FLOOR = 0.05

# Town demand from the public game rules. It is used only as a directional
# demand signal; actual prices remain authoritative.
SHOP_DEMAND = {
    "BAKERY": {"EGG", "WHEAT"},
    "PIZZA_SHOP": {"MILK", "TOMATO", "WHEAT"},
    "BRUNCH_SPOT": {"EGG", "WHEAT", "STRAWBERRY"},
    "YARN_STORE": {"WOOL"},
    "ICE_CREAM_SHOP": {"STRAWBERRY", "MILK", "WHEAT"},
    "PET_CAFE": {"CARROT"},
    "SMOOTHIE_SHOP": {"STRAWBERRY", "MILK"},
    "FARMERS_MARKET": {"WHEAT", "CARROT", "TOMATO", "STRAWBERRY"},
}

TOTAL_SEASON_DAYS = 30
LIQUIDATION_START_DAY = 25   # stop new long-horizon investment from here
FINAL_DUMP_DAY = 28          # dump everything in the shed regardless of price
CASH_RESERVE = 250           # never invest below this buffer
MAX_ANIMALS_V1 = 2           # cap animal expansion for now (keeps risk low)
MAX_LAND_QUADRANTS = 4       # was 3 — real competition replays show opponents
                              # scoring 10-16x higher ($70-95K vs our $8-13K)
                              # with the ENTIRE board densely farmed. Capping
                              # ourselves at 3/4 quadrants (75% of the land)
                              # was leaving a quarter of the map unused for
                              # the whole season. Testing the full 4/4 cap.
SHED_CAPACITY_DEFAULT = 100
LAND_COSTS = [1000, 2000, 4000]

_STATE = {
    "last_econ_day": -1,
    "focus_crops": ["WHEAT"],
    "invest_animal": None,
    "market_history": {},
    "opponent_history": [],
    "plan": {},
    "last_step": -1,
    "episode": 0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _count_crops(farm):
    counts = {}
    for row in farm["tiles"]:
        for tile in row:
            if isinstance(tile, dict) and tile.get("kind") == "PLANT":
                counts[tile["crop"]] = counts.get(tile["crop"], 0) + 1
    return counts


def _count_my_animals(farm):
    """Animals actually placed and producing on a tile."""
    n = 0
    for row in farm["tiles"]:
        for tile in row:
            if isinstance(tile, dict) and tile.get("kind") in ("COOP", "PASTURE") and tile.get("animal"):
                n += 1
    return n


def _count_owned_or_pending_animal(animal, farm, shed, inventories):
    """Placed on the farm, OR bought but not yet placed (sitting in the shed
    or in a unit's inventory mid-transit). Prevents runaway repurchasing."""
    n = 0
    for row in farm["tiles"]:
        for tile in row:
            if isinstance(tile, dict) and tile.get("animal") == animal:
                n += 1
    n += shed.get(animal, 0)
    for inv in inventories:
        n += inv.get(animal, 0)
    return n


def _wheat_feed_reserve(my_animal_count, days_remaining):
    """Wheat buffer sized for how many days of feeding actually still matter.
    If there isn't a single live animal on the farm yet, there is nothing to
    feed — reserving wheat "just in case" here starves the sell logic for
    weeks while an animal purchase is still in transit (bought but waiting
    on a structure). Only reserve once an animal is actually placed."""
    if my_animal_count == 0:
        return 0
    safety_days = min(3, max(1, days_remaining))
    return my_animal_count * safety_days + 3


def _empty_tile_ratio(farm):
    total, empty = 0, 0
    for row in farm["tiles"]:
        for tile in row:
            if tile == "LOCKED":
                continue
            total += 1
            if tile is None:
                empty += 1
    return (empty / total) if total else 0.0


def _shed_pos_candidates(board_size):
    half = board_size // 2
    return {(half - 1, half - 1), (half, half - 1), (half - 1, half), (half, half)}


def _is_shed_adjacent(pos, board_size):
    return tuple(pos) in _shed_pos_candidates(board_size)


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _step_towards(cur, target):
    cx, cy = cur
    tx, ty = target
    if cx == tx and cy == ty:
        return "PASS"
    if abs(tx - cx) >= abs(ty - cy):
        return "EAST" if tx > cx else "WEST"
    return "SOUTH" if ty > cy else "NORTH"



# ---------------------------------------------------------------------------
# Strategic intelligence / forecasting layer
# ---------------------------------------------------------------------------
def _reset_if_new_episode(obs):
    step = int(obs.get("step", 0) or 0)
    if step == 0 or step < _STATE.get("last_step", -1):
        _STATE["market_history"] = {}
        _STATE["opponent_history"] = []
        _STATE["plan"] = {}
        _STATE["last_econ_day"] = -1
        _STATE["episode"] = _STATE.get("episode", 0) + 1
    _STATE["last_step"] = step


def _record_market(obs):
    prices = obs.get("market", {}).get("prices", {}) or {}
    inv = obs.get("market", {}).get("inventory", {}) or {}
    for item, price in prices.items():
        hist = _STATE["market_history"].setdefault(item, [])
        hist.append((int(obs.get("day", 0)), int(obs.get("hour", 0)), float(price), float(inv.get(item, 0))))
        if len(hist) > MARKET_HISTORY_LEN:
            del hist[:-MARKET_HISTORY_LEN]


def _trend(item):
    h = _STATE["market_history"].get(item, [])
    if len(h) < 2:
        return 0.0
    p0 = h[-2][2]
    p1 = h[-1][2]
    if p0 <= 0:
        return 0.0
    return (p1 - p0) / p0


def _momentum(item):
    h = _STATE["market_history"].get(item, [])
    if len(h) < 4:
        return _trend(item)
    p_old = h[-4][2]
    p_new = h[-1][2]
    return (p_new - p_old) / p_old if p_old else 0.0


def _volatility(item):
    h = _STATE["market_history"].get(item, [])
    if len(h) < 4:
        return 0.0
    vals = [x[2] for x in h[-6:]]
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return 0.0
    return (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5 / mean


def _town_demand_score(obs, item):
    shops = (obs.get("town", {}) or {}).get("unlocked_shops", []) or []
    if not shops:
        return 0.0
    hits = sum(1 for shop in shops if item in SHOP_DEMAND.get(shop, set()))
    return min(1.0, hits / 3.0)


def _opponent_profile(obs, me_idx):
    opp = obs["farms"][1 - me_idx]
    counts = _count_crops(opp)
    animals = _count_my_animals(opp)
    land = len(opp.get("unlocked_quadrants", []))
    workers = 1 + len(opp.get("hands", []))
    total_crops = sum(counts.values()) or 1
    dominant_crop = max(counts, key=counts.get) if counts else None
    profile = {
        "animals": animals,
        "land": land,
        "workers": workers,
        "crop_counts": counts,
        "dominant_crop": dominant_crop,
        "crop_density": total_crops / max(1, 25 * land),
        "aggression": min(1.0, 0.25 * max(0, land - 1) + 0.12 * max(0, workers - 1)),
    }
    return profile


def _liquidity_state(me, days_remaining):
    cash = float(me.get("money", 0))
    # Cash runway is intentionally nonlinear: the final few days are more
    # expensive to get wrong than the opening days.
    runway_target = CASH_RESERVE + max(0, days_remaining - 3) * 35
    pressure = max(0.0, min(1.0, (runway_target - cash) / max(1.0, runway_target)))
    return {"cash": cash, "runway_target": runway_target, "pressure": pressure}


def _resource_bottleneck(me, private):
    empty_ratio = _empty_tile_ratio(me)
    workers = 1 + len(me.get("hands", []))
    crops = sum(_count_crops(me).values())
    # Approximate active-task load from the visible board. This avoids needing
    # a second simulator just to decide whether another worker is useful.
    watering = 0
    harvestable = 0
    for row in me["tiles"]:
        for tile in row:
            if isinstance(tile, dict) and tile.get("kind") == "PLANT":
                if not tile.get("watered_today", False):
                    watering += 1
                if tile.get("yield_units", 0) > 0:
                    harvestable += 1
    labor_load = watering + harvestable + sum(1 for v in private.get("shed", {}).values() if v > 0)
    if labor_load > workers * 5:
        labor = min(1.0, labor_load / max(1, workers * 12))
    else:
        labor = 0.25
    land = 1.0 - empty_ratio
    if land > 0.88 and workers < max(2, crops // 8):
        return "LAND"
    if labor > 0.65:
        return "LABOR"
    if float(me.get("money", 0)) < CASH_RESERVE + 300:
        return "CASH"
    return "MARKET"


def _forward_value_crop(crop, price, days_remaining, market_momentum=0.0, town_demand=0.0):
    info = CROPS[crop]
    if days_remaining < info["first_yield_day"] + 1:
        return -1e9
    # Approximate number of monetizable production days. Ongoing crops retain
    # value after first yield; one-time crops are capped by max_yield_day.
    active_days = max(0, days_remaining - info["first_yield_day"])
    if info["ongoing"]:
        active_days = min(active_days, 8)
    else:
        active_days = min(active_days, max(1, info["max_yield_day"] - info["first_yield_day"] + 1))
    trend_mult = max(0.65, min(1.35, 1.0 + 1.5 * market_momentum))
    demand_mult = 1.0 + 0.10 * town_demand
    # Premium goods can crash toward the $1 floor under glut. Keep an
    # uncertainty penalty instead of treating the base quote as guaranteed.
    risk_mult = 0.72 if crop == "MELON" else 0.92 if crop in PREMIUM_CROPS else 0.97
    expected_revenue = YIELD_PER_TILE_DAY[crop] * price * active_days * trend_mult * demand_mult * risk_mult
    cost = info["seed"] + 0.25 * active_days  # small labor/time proxy
    return expected_revenue - cost


def _forward_value_animal(animal, prices, days_remaining):
    info = ANIMALS[animal]
    if days_remaining < 7:
        return -1e9
    product = info["product"]
    price = prices.get(product, PRODUCT_BASE_PRICE[product])
    wheat = prices.get("WHEAT", PRODUCT_BASE_PRICE["WHEAT"])
    # Feed is one wheat/day in the full game's animal loop. CARE/bonus and
    # fertilizer are intentionally treated as upside, not guaranteed revenue.
    daily = info["yield_per_day"] * price - wheat
    useful_days = max(0, days_remaining - 1)
    gross = daily * useful_days
    return gross - info["cost"] - BUILD_COST[info["structure"]]



# ---------------------------------------------------------------------------
# Rule-faithful economic simulator + beam search
# ---------------------------------------------------------------------------
# The official interpreter is the authority for the live game.  This simulator
# mirrors its economic rules (crop/animal timing, feed, care, fertilizer,
# market-price curve, land/hire costs and town consumption) but intentionally
# omits pixel-level movement.  Movement remains handled by the tactical layer.
# This keeps the search fast enough for a 1-second agent while making the
# capital planner materially more realistic than the previous heuristic model.
MARKET_I0 = 10000
PRICE_FLOOR = 1
HINGE_GAIN = 8.0
MARKET_PARAMS = {
    "WHEAT": {"base":25,"I0":MARKET_I0,"T":400,"below_func":"sqrt","below_target":0.80,"above_func":"log","above_target":0.20},
    "CARROT":{"base":35,"I0":MARKET_I0,"T":450,"below_func":"hinge","below_target":1.00,"above_func":"sqrt","above_target":0.70},
    "TOMATO":{"base":60,"I0":MARKET_I0,"T":200,"below_func":"hinge","below_target":0.40,"above_func":"sqrt","above_target":0.60},
    "STRAWBERRY":{"base":120,"I0":MARKET_I0,"T":100,"below_func":"sqrt","below_target":0.70,"above_func":"linear","above_target":1.60},
    "MELON":{"base":250,"I0":MARKET_I0,"T":300,"below_func":"log","below_target":0.20,"above_func":"sq","above_target":3.60},
    "EGG":{"base":50,"I0":MARKET_I0,"T":332,"below_func":"hinge","below_target":0.40,"above_func":"log","above_target":0.20},
    "MILK":{"base":160,"I0":MARKET_I0,"T":122,"below_func":"sqrt","below_target":0.60,"above_func":"linear","above_target":1.60},
    "WOOL":{"base":200,"I0":MARKET_I0,"T":105,"below_func":"log","below_target":0.20,"above_func":"sq","above_target":3.20},
    "FERTILIZER":{"base":100,"I0":MARKET_I0,"T":200,"below_func":"linear","below_target":0.40,"above_func":"linear","above_target":0.40},
}
SHOP_DEMAND = {
    "BAKERY":{"EGG","WHEAT"}, "PIZZA_SHOP":{"MILK","TOMATO","WHEAT"},
    "BRUNCH_SPOT":{"EGG","WHEAT","STRAWBERRY"}, "YARN_STORE":{"WOOL"},
    "ICE_CREAM_SHOP":{"STRAWBERRY","MILK","WHEAT"}, "PET_CAFE":{"CARROT"},
    "SMOOTHIE_SHOP":{"STRAWBERRY","MILK"}, "FARMERS_MARKET":{"WHEAT","CARROT","TOMATO","STRAWBERRY"},
}
PRODUCTS = ["WHEAT","CARROT","TOMATO","STRAWBERRY","MELON","EGG","MILK","WOOL","FERTILIZER"]


def _sim_shape(func, x, T=None):
    x=max(0.0,float(x))
    if func == "linear": return x
    if func == "sq": return x*x
    if func == "sqrt": return x**0.5
    if func == "log":
        import math
        return math.log(1.0+x)
    if func == "log10":
        import math
        return math.log10(1.0+x)
    if func == "hinge":
        if not T or T <= 0: return x
        u=x/T
        return u + HINGE_GAIN*max(0.0,u-1.0)**2
    return x


def _sim_market_price(item, inventory):
    p=MARKET_PARAMS[item]
    base,I0,T=p["base"],p["I0"],p["T"]
    if inventory < I0:
        f=p["below_func"]
        amp=p["below_target"]*base/max(1e-12,_sim_shape(f,T,T))
        price=base+amp*_sim_shape(f,I0-inventory,T)
    else:
        f=p["above_func"]
        amp=p["above_target"]*base/max(1e-12,_sim_shape(f,T,T))
        price=base-amp*_sim_shape(f,inventory-I0,T)
    return max(PRICE_FLOOR,int(round(price)))


def _sim_sell_price(item, inv):
    return _sim_market_price(item, inv)


def _sim_demand_for_day(day, unlocked_shops):
    demand={p:1 for p in PRODUCTS if p != "FERTILIZER"}
    # Advanced defaults: town center every 24 steps, represented here at
    # day boundaries; the planner also models shop demand every 4 turns.
    if day >= 10:
        for p in list(demand): demand[p] += 1
    if day >= 20:
        for p in list(demand): demand[p] += 2
    for shop in unlocked_shops:
        for item in SHOP_DEMAND.get(shop,()):
            demand[item]=demand.get(item,0)+6
    return demand


def _sim_unlock_shops(day, seed=0):
    # Deterministic expectation rather than pretending to know future RNG.
    # Beam search evaluates several shop-demand regimes separately.
    n=min(8, day//3)
    names=sorted(SHOP_DEMAND)
    if not names or n<=0: return []
    return [names[(seed+i*3)%len(names)] for i in range(n)]


def _sim_copy_state(state):
    return {
        "cash":float(state["cash"]), "market":dict(state["market"]),
        "crops":list(state["crops"]), "animals":list(state["animals"]),
        "wheat":int(state["wheat"]), "shed":dict(state["shed"]),
        "land":int(state["land"]), "hands":int(state["hands"]),
        "fert":int(state.get("fert",0)), "score":float(state.get("score",0.0)),
    }


def _sim_initial_state(obs, me_idx):
    me=obs["farms"][me_idx]
    shed=obs["private"].get("shed",{})
    crops=[]
    animals=[]
    for row in me.get("tiles",[]):
        for tile in row:
            if isinstance(tile,dict):
                if tile.get("kind")=="PLANT":
                    crops.append({"crop":tile["crop"],"age":max(0,int(obs.get("day",0)-tile.get("planted_day",obs.get("day",0)))),"yield":int(tile.get("yield_units",0))})
                elif tile.get("animal"):
                    animals.append({"animal":tile["animal"],"age":max(0,int(obs.get("day",0)-tile.get("placed_day",obs.get("day",0)))),"yield":int(tile.get("yield_units",0)),"fed":True,"care":False})
    return {
        "cash":float(me.get("money",0)),
        "market":dict(obs["market"].get("inventory",{})),
        "crops":crops,
        "animals":animals,
        "wheat":int(shed.get("WHEAT",0)),
        "shed":dict(shed),
        "land":len(me.get("unlocked_quadrants",["NW"])),
        "hands":1+len(me.get("hands",[])),
        "fert":int(shed.get("FERTILIZER",0)),
        "score":0.0,
    }


def _sim_production_day(state, day, shop_seed=0):
    # One daily economic transition. The live interpreter refreshes production
    # at end-of-day; these formulas mirror that timing.
    # Crops
    new_crops=[]
    for c in state["crops"]:
        c=dict(c); c["age"]+=1
        info=CROPS[c["crop"]]
        if info["ongoing"]:
            ds=c["age"]-info["first_yield_day"]
            if ds>=0 and ds % info["interval"]==0:
                production=ds//info["interval"]+1
                if production<=info["max_yield"]:
                    # Assume the tactical layer keeps the crop watered.
                    c["yield"] += 1
        else:
            # One-time crops start with one seed unit. Optimal watering in the
            # official bonus window adds one unit/day, capped at max_yield.
            window_start=(info["max_yield_day"]+1)//2
            if window_start <= c["age"] <= info["max_yield_day"]:
                c["yield"] = min(info["max_yield"], c["yield"]+1)
        new_crops.append(c)
    state["crops"]=new_crops

    # Animals: one wheat per animal per day. Missed feeding twice would remove
    # an animal in the live game; the planner therefore treats a wheat shortage
    # as an explicit escape penalty rather than free production.
    survivors=[]
    for a in state["animals"]:
        a=dict(a); a["age"]+=1
        if state["wheat"]>0:
            state["wheat"]-=1
            a["fed"] = True
            a["care"] = True
        else:
            a["fed"] = False
            a["missed"] = a.get("missed",0)+1
        info=ANIMALS[a["animal"]]
        ds=a["age"]-info["first_yield_day"]
        if a["fed"] and ds>=0 and ds % info["interval"]==0:
            a["yield"] = min(info["max_held"], a["yield"]+1+(1 if a.get("care") else 0))
            a["care"]=False
        else:
            a["care"]=False
        if a.get("missed",0)<2:
            survivors.append(a)
    state["animals"]=survivors

    # Town consumption lowers inventory and can lift future prices.
    demand=_sim_demand_for_day(day,_sim_unlock_shops(day,shop_seed))
    for item,n in demand.items():
        state["market"][item]=state["market"].get(item,MARKET_I0)-n


def _sim_collect_and_sell(state, sell_fraction=0.75):
    # Harvest everything mature enough, then sell a controlled fraction. The
    # real tactical executor decides exact collection timing.
    for c in state["crops"]:
        info=CROPS[c["crop"]]
        if c["age"]>=info["first_yield_day"] and c["yield"]>0:
            state["shed"][c["crop"]]=state["shed"].get(c["crop"],0)+c["yield"]
            c["yield"]=0
    for a in state["animals"]:
        product=ANIMALS[a["animal"]]["product"]
        if a["yield"]>0:
            state["shed"][product]=state["shed"].get(product,0)+a["yield"]
            a["yield"]=0
    for item,qty in list(state["shed"].items()):
        if item in ANIMALS or item=="FERTILIZER" or qty<=0: continue
        n=int(qty*sell_fraction)
        if item=="WHEAT":
            # Keep a modest two-day safety buffer per live animal.
            reserve=2*len(state["animals"])
            n=min(n,max(0,qty-reserve))
        if n<=0: continue
        price=_sim_sell_price(item,state["market"].get(item,MARKET_I0))
        state["cash"] += n*price
        state["market"][item] += n if price>1 else 0
        state["shed"][item]-=n
        state["score"] += n*price


def _sim_buy_crop(state,crop,n=1):
    cost=CROPS[crop]["seed"]*n
    if state["cash"]<cost: return False
    state["cash"]-=cost
    state["crops"].extend({"crop":crop,"age":0,"yield":1} for _ in range(n))
    return True


def _sim_buy_animal(state,animal,n=1):
    cost=ANIMALS[animal]["cost"]*n
    if state["cash"]<cost: return False
    state["cash"]-=cost
    state["animals"].extend({"animal":animal,"age":0,"yield":0,"fed":True,"care":False} for _ in range(n))
    return True


def _sim_action(state, action):
    kind,name=action
    if kind=="CROP": return _sim_buy_crop(state,name,1)
    if kind=="ANIMAL": return _sim_buy_animal(state,name,1)
    if kind=="LAND":
        idx=state["land"]-1
        if idx>=3: return False
        cost=LAND_COSTS[idx]
        if state["cash"]<cost: return False
        state["cash"]-=cost; state["land"]+=1; return True
    if kind=="HIRE":
        # Fibonacci cost, indexed by hires today; day-level simulator treats
        # one strategic hire per day as the marginal decision.
        n=max(0,state.get("hires_today",0))
        a,b=1,1
        for _ in range(n): a,b=b,a+b
        if state["cash"]<a: return False
        state["cash"]-=a; state["hands"]+=1; state["hires_today"]=n+1; return True
    return False


def _sim_terminal_value(state, days_left):
    # Liquidation value is based on current quote; unsold goods do count in
    # final money only if actually sold, so terminal inventory is converted to
    # cash here.
    value=state["cash"]
    for item,qty in state["shed"].items():
        if item in ANIMALS or item=="FERTILIZER": continue
        value += qty*_sim_sell_price(item,state["market"].get(item,MARKET_I0))
    # Plants/animals with a short remaining payback window retain some value.
    for c in state["crops"]:
        info=CROPS[c["crop"]]
        if c["age"]<info["first_yield_day"]:
            value += max(0,days_left-(info["first_yield_day"]-c["age"])) * CROPS[c["crop"]]["base_price"]*0.2
    return value


def _beam_simulate(obs, me_idx, days_remaining, beam_width=10, horizon=10, scenarios=3):
    """Monte-Carlo/beam search over strategic capital choices.

    The beam branches only on high-level investments. Every branch is rolled
    through the rule-faithful economic transition for several days and then
    marked-to-market. Multiple shop-demand scenarios provide cheap Monte-Carlo
    robustness without blowing the action-time budget.
    """
    base=_sim_initial_state(obs,me_idx)
    horizon=min(horizon,max(1,days_remaining))
    candidates=[("CROP",c) for c in CROPS if c!="WHEAT"] + [("CROP","WHEAT")]
    candidates += [("ANIMAL",a) for a in ANIMALS]
    if base["land"]<4: candidates.append(("LAND",None))
    candidates.append(("HIRE",None))
    all_scores=[]
    for first in candidates:
        vals=[]
        for scenario in range(max(1,scenarios)):
            st=_sim_copy_state(base)
            st["hires_today"]=0
            ok=_sim_action(st,first)
            if not ok:
                vals.append(-1e9); continue
            for d in range(horizon):
                # Reinvest only when cash is comfortably above reserve. The
                # branch itself is intentionally sparse to keep beam search fast.
                if d>0 and st["cash"]>CASH_RESERVE+120:
                    # choose the best current marginal crop only on days 0,3,6
                    if d in (3,6,9):
                        best_crop=max(CROPS,key=lambda c:_sim_market_price(c,st["market"].get(c,MARKET_I0))*YIELD_PER_TILE_DAY[c]-CROPS[c]["seed"])
                        if best_crop and (len(st["crops"])+len(st["animals"])) < 25:
                            _sim_buy_crop(st,best_crop,1)
                if d % 2 == 0:
                    _sim_collect_and_sell(st,0.55)
                _sim_production_day(st, int(obs.get("day",0))+d, scenario)
            _sim_collect_and_sell(st,1.0)
            vals.append(_sim_terminal_value(st,max(0,days_remaining-horizon)))
        all_scores.append((sum(vals)/len(vals),first,vals))
    all_scores.sort(reverse=True,key=lambda x:x[0])
    best=all_scores[0] if all_scores else (-1e9,(None,None),[])
    second=all_scores[1] if len(all_scores)>1 else (-1e9,(None,None),[])
    gap=max(0.0,best[0]-second[0])
    conf=max(0.05,min(0.99,0.5+gap/(abs(best[0])+500.0)))
    return {"best":(best[0],best[1][0],best[1][1]),"runner_up":(second[0],second[1][0],second[1][1]),
            "confidence":conf,"bottleneck":_resource_bottleneck(obs["farms"][me_idx],obs["private"]),
            "candidates":[(v,k,n) for v,(k,n),_ in all_scores[:beam_width]]}


def _simulate_capital_choices(obs, me_idx, days_remaining):
    return _beam_simulate(obs,me_idx,days_remaining,beam_width=10,horizon=min(10,MAX_FORWARD_DAYS+4),scenarios=3)

def _strategic_plan(obs, me_idx, days_remaining):
    me = obs["farms"][me_idx]
    private = obs["private"]
    prices = obs["market"]["prices"]
    opp = _opponent_profile(obs, me_idx)
    liquidity = _liquidity_state(me, days_remaining)
    sim = _simulate_capital_choices(obs, me_idx, days_remaining)

    # Score crops with current quote + momentum + town demand + opponent share.
    counts = _count_crops(me)
    opp_counts = opp["crop_counts"]
    ranked = []
    for crop, info in CROPS.items():
        quote = prices.get(crop, info["base_price"])
        value = _forward_value_crop(crop, quote, days_remaining, _momentum(crop), _town_demand_score(obs, crop))
        total_crop = counts.get(crop, 0) + opp_counts.get(crop, 0)
        my_share = counts.get(crop, 0) / max(1, total_crop)
        opponent_share = opp_counts.get(crop, 0) / max(1, total_crop)
        # Diversify away from a market where both players are already flooding.
        value *= max(0.65, 1.0 - 0.30 * my_share)
        value *= max(0.70, 1.0 - 0.25 * opponent_share)
        if crop == "WHEAT" and _count_my_animals(me) > 0:
            value += 30
        ranked.append((value, crop))
    ranked.sort(reverse=True)
    focus = [c for _, c in ranked if _ > 0][:2] or ["WHEAT"]

    # Animal choice from forward value, but never if liquidity/endgame makes
    # the payback window too short.
    animal_choice = None
    animal_values = sorted(((_forward_value_animal(a, prices, days_remaining), a) for a in ANIMALS), reverse=True)
    if animal_values and animal_values[0][0] > 0 and days_remaining >= 10 and liquidity["pressure"] < 0.35:
        animal_choice = animal_values[0][1]

    # Competitive route prior.
    #
    # The strongest public research on this environment consistently converges
    # on the same economic shape: cows + sheep + CARE, feed wheat, and a
    # controlled strawberry book.  The old beam layer could repeatedly choose
    # locally attractive geese/melon branches and starve the long-lived herd.
    # Keep the beam as a tie-breaker, but constrain it to the economically
    # viable family instead of allowing a cheap-but-bad local optimum.
    sim_kind = sim["best"][1]
    sim_name = sim["best"][2]
    animal_counts = _v4_count_animals(me)
    total_herd = animal_counts.get("COW", 0) + animal_counts.get("SHEEP", 0)

    if days_remaining > 11:
        if animal_counts.get("COW", 0) < 2:
            animal_choice = "COW"
        else:
            animal_choice = None
        # Wheat is both feed and a low-volatility cash-flow crop.  Strawberry
        # is the premium engine once the herd is established.
        focus = ["WHEAT", "STRAWBERRY"] if total_herd > 0 else ["WHEAT", "STRAWBERRY"]
    elif days_remaining > 6:
        animal_choice = None
        focus = ["WHEAT", "STRAWBERRY"]
    else:
        animal_choice = None
        focus = ["WHEAT"]

    # Only let the simulator override the crop/animal family when it agrees
    # with the competitive route.  This keeps adaptation while preventing a
    # one-step price spike from destroying the season-long economy.
    if sim.get("confidence", 0) >= 0.78 and days_remaining > 14:
        if sim_kind == "ANIMAL" and sim_name in ("COW", "SHEEP"):
            if sim_name == "COW" and animal_counts.get("COW", 0) < 8:
                animal_choice = "COW"
            elif sim_name == "SHEEP" and animal_counts.get("COW", 0) >= 8 and animal_counts.get("SHEEP", 0) < 6:
                animal_choice = "SHEEP"

    # Never enter a long-payback purchase window near the end.
    if days_remaining <= 10:
        animal_choice = None

    # Competitive mode: when behind, permit more variance; when ahead, lock
    # down the bankroll and sell into strength.
    score_gap = float(me.get("money", 0)) - float(obs["farms"][1 - me_idx].get("money", 0))
    if score_gap > 500:
        risk_budget = 0.25
    elif score_gap < -500:
        risk_budget = 0.75
    else:
        risk_budget = 0.50

    _STATE["opponent_history"].append({"day": obs.get("day", 0), "gap": score_gap, "profile": opp})
    if len(_STATE["opponent_history"]) > 20:
        del _STATE["opponent_history"][:-20]

    return {
        "focus_crops": focus,
        "invest_animal": animal_choice,
        "bottleneck": sim["bottleneck"],
        "confidence": sim["confidence"],
        "risk_budget": risk_budget,
        "score_gap": score_gap,
        "opponent": opp,
        "simulation": sim,
        "liquidity": liquidity,
    }


# ---------------------------------------------------------------------------
# 1. Economic Decision Engine
# ---------------------------------------------------------------------------
def _run_economic_engine(obs, me_idx, days_remaining):
    plan = _strategic_plan(obs, me_idx, days_remaining)
    _STATE["plan"] = plan
    return plan["focus_crops"], plan["invest_animal"]


# ---------------------------------------------------------------------------
# 2. Target scanning (tactical layer)
# ---------------------------------------------------------------------------
def _find_targets(board, day, days_remaining, focus_crops, seeds, want_animal, owned_or_pending_want, total_workers=1):
    targets = []
    board_size = len(board)
    has_empty_structure = {"COOP": False, "PASTURE": False}

    for y in range(board_size):
        for x in range(board_size):
            tile = board[y][x]
            if not isinstance(tile, dict):
                continue
            kind = tile.get("kind")

            if kind == "PLANT":
                crop_info = CROPS[tile["crop"]]
                age_days = day - tile["planted_day"]
                ready_to_harvest = age_days >= crop_info["first_yield_day"]
                needs_water = not tile.get("watered_today", False)

                if needs_water:
                    tier = 0 if tile.get("consecutive_unwatered", 0) >= 1 else 1
                    targets.append((tier, x, y, "WATER"))
                elif tile.get("yield_units", 0) > 0 and ready_to_harvest:
                    targets.append((2, x, y, "HARVEST"))
                elif (
                    tile["crop"] in PREMIUM_CROPS
                    and tile.get("fertilized_until_day", -1) < day
                    and days_remaining >= 3
                ):
                    targets.append((3, x, y, "FERTILIZE"))
                continue

            if "animal" in tile and tile.get("animal"):
                if tile.get("yield_units", 0) > 0:
                    targets.append((1, x, y, "HARVEST_ANIMAL"))
                if not tile.get("fed_today", False):
                    # Feeding an about-to-escape animal outranks even an
                    # about-to-weed plant: losing a $300-500 animal is
                    # permanent, losing a plant tile to a weed costs a DIG +
                    # a new $10-100 seed. Same urgency band, feed wins ties.
                    tier = -1 if tile.get("consecutive_unfed", 0) >= 1 else 1
                    targets.append((tier, x, y, "FEED"))
                if tile.get("fertilizer_available", False):
                    targets.append((4, x, y, "COLLECT_FERTILIZER"))
                if not tile.get("cared_today", False):
                    targets.append((4, x, y, "CARE"))
                continue

            if kind in ("COOP", "PASTURE") and not tile.get("animal"):
                has_empty_structure[kind] = True
                targets.append((5, x, y, "PLACE_ANIMAL_" + kind))
                continue

            if kind == "WEED":
                targets.append((6, x, y, "DIG"))
                continue

    # Build a new structure only if the economic engine wants an animal, we're
    # under the v1 cap counting animals we already own OR have bought-but-not-
    # -yet-placed (not just placed ones — otherwise we redundantly build a
    # second structure while the first purchased animal is still in transit),
    # AND there isn't already an empty matching structure sitting unused.
    if (
        want_animal
        and owned_or_pending_want < MAX_ANIMALS_V1
        and days_remaining >= 10
        and not has_empty_structure[ANIMALS[want_animal]["structure"]]
    ):
        structure = ANIMALS[want_animal]["structure"]
        for y in range(board_size):
            for x in range(board_size):
                if board[y][x] is None:
                    targets.append((5, x, y, "BUILD_" + structure))
                    break
            else:
                continue
            break

    # Plant focus crops on empty tiles — gated per-crop by its OWN maturity,
    # not a flat number. Planting melon (first_yield_day=10) with 3 days left
    # in the season burns the $80 seed cost for a harvest that can never
    # happen; wheat (first_yield_day=2) is fine right up to the wire.
    #
    # ALSO gated by workforce coverage capacity: a real Kaggle validation
    # replay showed weeds climbing from 3 to 62 tiles over one season with 7
    # workers (1 farmer + 6 hands) on 3 unlocked quadrants (75 tiles) — the
    # planting loop kept adding new tiles as fast as seeds allowed, far
    # outpacing how many tiles that many workers can actually water every
    # single day. Each worker can realistically water/tend at most ~9 tiles
    # a day once travel time on a 10x10 board is accounted for; planting
    # beyond that just buys weeds, not crops.
    total_workers = max(1, total_workers)
    coverage_capacity = total_workers * 14
    alive_plants = sum(
        1 for row in board for t in row
        if isinstance(t, dict) and t.get("kind") == "PLANT"
    )
    plant_room = max(0, coverage_capacity - alive_plants)

    for crop in focus_crops:
        if plant_room <= 0:
            break
        if seeds.get(crop, 0) <= 0:
            continue
        min_days_needed = CROPS[crop]["first_yield_day"] + 1  # +1 turnaround buffer
        if days_remaining < min_days_needed:
            continue
        for y in range(board_size):
            if plant_room <= 0:
                break
            for x in range(board_size):
                if plant_room <= 0:
                    break
                if board[y][x] is None:
                    targets.append((5, x, y, "PLANT_" + crop))
                    plant_room -= 1

    targets.sort(key=lambda t: t[0])
    return targets


def _act_on_tile(action_hint, inv):
    if action_hint in ("HARVEST", "HARVEST_ANIMAL"):
        return ["HARVEST"]
    if action_hint == "WATER":
        return ["WATER"]
    if action_hint == "FERTILIZE":
        if inv.get("FERTILIZER", 0) > 0:
            return ["FERTILIZE"]
        return None
    if action_hint == "FEED":
        if inv.get("WHEAT", 0) > 0:
            return ["FEED"]
        return None
    if action_hint == "COLLECT_FERTILIZER":
        return ["COLLECT_FERTILIZER"]
    if action_hint == "CARE":
        return ["CARE"]
    if action_hint == "DIG":
        return ["DIG"]
    if action_hint.startswith("BUILD_"):
        return [action_hint]  # BUILD_COOP / BUILD_PASTURE
    if action_hint.startswith("PLACE_ANIMAL_"):
        structure = action_hint.replace("PLACE_ANIMAL_", "")
        for animal, info in ANIMALS.items():
            if inv.get(animal, 0) > 0 and info["structure"] == structure:
                return ["PLACE", animal]
        return None
    if action_hint.startswith("PLANT_"):
        return ["PLANT", action_hint.replace("PLANT_", "")]
    return None




def _target_priority_with_strategy(target, pos, plan, me):
    """Turn tactical priorities into economic priorities without losing safety.
    Urgent water/feed still wins; otherwise the planner prefers actions that
    unlock inventory/cash and reduces walking when several tasks are equal.
    """
    base, tx, ty, hint = target
    d = _manhattan(pos, (tx, ty))
    bonus = 0.0
    bottleneck = plan.get("bottleneck")
    if hint == "HARVEST" and bottleneck in ("CASH", "MARKET"):
        bonus -= 0.35
    if hint == "WATER":
        bonus -= 2.0
    if hint == "FEED":
        bonus -= 2.2
    if hint == "CARE" and _count_my_animals(me) > 0:
        bonus -= 0.25
    if hint.startswith("PLANT_"):
        crop = hint.replace("PLANT_", "")
        if crop in plan.get("focus_crops", []):
            bonus -= 0.30
    # A tiny route penalty makes the unit cluster work instead of zig-zagging.
    return (base + bonus, d)


def _sell_policy(item, price, amount, obs, plan, final_dump, in_liquidation):
    if amount <= 0:
        return 0
    if final_dump:
        return amount
    momentum = _momentum(item)
    vol = _volatility(item)
    demand = _town_demand_score(obs, item)
    # Strong upward momentum + demand = wait. Strong negative momentum = sell.
    if in_liquidation:
        threshold = 0.12
    else:
        threshold = 0.55
    ratio = threshold
    if momentum > 0.04 and demand > 0.25:
        ratio += 0.20
    if momentum < -0.04:
        ratio -= 0.20
    if vol > 0.18:
        ratio -= 0.08
    if plan.get("score_gap", 0) > 500:
        ratio -= 0.05
    ratio = max(0.10, min(0.90, ratio))
    # Premium products are extremely glut-sensitive.  Selling a large fraction
    # of the shed in one order can destroy the very price we are trying to
    # capture.  Use small drip batches for premium goods and bulk batches for
    # low-volatility staples.
    batch_cap = {
        "WHEAT": 24,
        "CARROT": 14,
        "TOMATO": 8,
        "STRAWBERRY": 4,
        "MELON": 2,
        "EGG": 10,
        "MILK": 3,
        "WOOL": 3,
    }.get(item, 5)
    if item in ("STRAWBERRY", "MILK", "WOOL", "MELON"):
        # Sell more aggressively only when the current quote is already weak;
        # otherwise preserve inventory for the next demand/market refresh.
        if price > PRODUCT_BASE_PRICE.get(item, price) * 0.92 and momentum >= -0.02:
            batch_cap = max(1, batch_cap // 2)
    # Never liquidate the strategic wheat reserve.
    if item == "WHEAT":
        reserve = plan.get("wheat_reserve", 0)
        sellable = max(0, amount - reserve)
    else:
        sellable = amount
    return max(0, min(sellable, batch_cap, int(round(sellable * ratio))))

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def agent(obs):
    _reset_if_new_episode(obs)
    _record_market(obs)

    player = obs["player"]
    farms = obs["farms"]
    me = farms[player]
    private = obs["private"]
    day = obs["day"]
    market = obs["market"]
    prices = market["prices"]
    shed = private.get("shed", {})
    seeds = private.get("seeds", {})
    board_size = len(me["tiles"])
    money = me["money"]
    days_remaining = max(TOTAL_SEASON_DAYS - day, 0)
    in_liquidation = day >= LIQUIDATION_START_DAY
    final_dump = day >= FINAL_DUMP_DAY

    # ---- Economic engine: re-evaluate once/day ----
    if _STATE["last_econ_day"] != day:
        focus_crops, invest_animal = _run_economic_engine(obs, player, days_remaining)
        if in_liquidation:
            invest_animal = None  # no new animals this late, can't pay back
        _STATE["focus_crops"] = focus_crops
        _STATE["invest_animal"] = invest_animal
        _STATE["last_econ_day"] = day
    focus_crops = _STATE["focus_crops"]
    invest_animal = _STATE["invest_animal"]
    my_animal_count = _count_my_animals(me)
    plan = _STATE.get("plan", {}) or {}
    plan["wheat_reserve"] = _wheat_feed_reserve(my_animal_count, days_remaining)
    _STATE["plan"] = plan

    # ---- Build unit list ----
    units = [tuple(me["farmer"])] + [tuple(h) for h in me["hands"]]
    inventories = private.get("inventories", [{}])
    while len(inventories) < len(units):
        inventories.append({})

    owned_or_pending_want = (
        _count_owned_or_pending_animal(invest_animal, me, shed, inventories)
        if invest_animal else 0
    )
    targets = _find_targets(
        me["tiles"], day, days_remaining, focus_crops, seeds, invest_animal, owned_or_pending_want,
        total_workers=len(units),
    )
    used_targets = set()

    farmer_action = ["PASS"]
    hands_actions = []

    for idx, pos in enumerate(units):
        inv = inventories[idx] if idx < len(inventories) else {}
        chosen_action = None

        # Final-day safety net: SELL only ever draws from the shed, never from
        # a unit's carried inventory — so anything harvested but still "in
        # someone's hands" when the season ends is stranded value (day-end
        # auto-drops it to shed, but by then there are no turns left to sell
        # it). On the last day, carrying a sellable item overrides every
        # other task: head straight for the shed so DROP+SELL can still fire.
        if final_dump and not _is_shed_adjacent(pos, board_size):
            carrying_sellable = any(inv.get(k, 0) > 0 for k in PRODUCT_BASE_PRICE if k != "FERTILIZER")
            if carrying_sellable:
                nearest_shed = min(
                    _shed_pos_candidates(board_size),
                    key=lambda s: _manhattan(pos, s),
                )
                chosen_action = [_step_towards(pos, nearest_shed)]

        if chosen_action is None and _is_shed_adjacent(pos, board_size):
            carrying_extra = any(k not in ("WHEAT", "FERTILIZER") and inv.get(k, 0) > 0 for k in inv if k not in ANIMALS)
            carrying_animal = any(inv.get(a, 0) > 0 for a in ANIMALS)
            has_target_structure = carrying_animal and any(
                isinstance(t, dict) and t.get("kind") == info["structure"] and not t.get("animal")
                for row_ in me["tiles"] for t in row_
                for a, info in ANIMALS.items() if inv.get(a, 0) > 0
            )
            if carrying_extra:
                # Drop harvested goods regardless of also carrying an animal —
                # an animal with no structure yet must never block the rest of
                # the inventory from reaching the shed to be sold.
                chosen_action = ["DROP"]
            elif carrying_animal and not has_target_structure:
                # No matching empty structure exists anywhere on the farm yet:
                # carrying this animal around is pointless, drop it back so it
                # stops occupying the unit's inventory slot every single turn.
                chosen_action = ["DROP"]
            elif invest_animal and shed.get(invest_animal, 0) > 0 and not carrying_animal:
                chosen_action = ["PICKUP", invest_animal, 1]
            elif my_animal_count > 0 and inv.get("WHEAT", 0) == 0 and shed.get("WHEAT", 0) > 0:
                # Only pre-fetch feed wheat if there's an actual live animal on
                # the farm that could need it — otherwise this wheat just sits
                # uselessly in a unit's pocket, tricking the Wheat Reserve
                # Manager into thinking the shed is short and re-buying.
                chosen_action = ["PICKUP", "WHEAT", min(5, shed.get("WHEAT", 0))]
            elif inv.get("FERTILIZER", 0) == 0 and shed.get("FERTILIZER", 0) > 0:
                chosen_action = ["PICKUP", "FERTILIZER", 1]

        if chosen_action is None:
            # A unit already carrying an animal must deliver it before taking
            # on any other task — otherwise it keeps grabbing whatever WATER
            # task is globally most urgent and drags the animal around for
            # days, since PLACE_ANIMAL only competes at the low priority tier.
            # This is a personal override for THIS unit only; it doesn't
            # change the tier for other units' target selection.
            carried_animal = next((a for a in ANIMALS if inv.get(a, 0) > 0), None)
            if carried_animal is not None:
                needed_structure = ANIMALS[carried_animal]["structure"]
                best_struct, best_d = None, None
                for y in range(board_size):
                    for x in range(board_size):
                        t = me["tiles"][y][x]
                        if isinstance(t, dict) and t.get("kind") == needed_structure and not t.get("animal"):
                            if (x, y) in used_targets:
                                continue
                            d = _manhattan(pos, (x, y))
                            if best_d is None or d < best_d:
                                best_d, best_struct = d, (x, y)
                if best_struct is not None:
                    used_targets.add(best_struct)
                    if pos == best_struct:
                        chosen_action = ["PLACE", carried_animal]
                    else:
                        chosen_action = [_step_towards(pos, best_struct)]

        if chosen_action is None:
            best_score, best_target = None, None
            for t in targets:
                key = (t[1], t[2])
                if key in used_targets:
                    continue
                hint = t[3]
                if hint.startswith("PLACE_ANIMAL_"):
                    structure = hint.replace("PLACE_ANIMAL_", "")
                    if not any(inv.get(a, 0) > 0 and info["structure"] == structure for a, info in ANIMALS.items()):
                        continue
                if hint == "FEED" and inv.get("WHEAT", 0) <= 0:
                    continue
                if hint == "FERTILIZE" and inv.get("FERTILIZER", 0) <= 0:
                    continue
                score = _target_priority_with_strategy(t, pos, plan, me)
                if best_score is None or score < best_score:
                    best_score, best_target = score, t
            if best_target is not None:
                tx, ty, hint = best_target[1], best_target[2], best_target[3]
                if pos == (tx, ty):
                    act = _act_on_tile(hint, inv)
                    if act:
                        chosen_action = act
                        used_targets.add((tx, ty))
                else:
                    chosen_action = [_step_towards(pos, (tx, ty))]
                    used_targets.add((tx, ty))

        if chosen_action is None:
            chosen_action = ["PASS"]

        if idx == 0:
            farmer_action = chosen_action
        else:
            hands_actions.append(chosen_action)

    # ---- Market layer ----
    market_orders = []
    total_shed = sum(v for k, v in shed.items() if k not in ANIMALS)
    # Running projected balance: every check below spends against this, not
    # against the original `money`, so we never queue orders whose combined
    # cost exceeds what's actually available this turn.
    projected_money = money

    # -- Strategic selling: momentum + town demand + endgame --
    # Current market prices are authoritative. History is only a timing signal.
    if total_shed > 0:
        for item, qty in list(shed.items()):
            if item in ANIMALS or item == "FERTILIZER" or qty <= 0:
                continue
            n = _sell_policy(item, prices.get(item, PRODUCT_BASE_PRICE.get(item, 1)), qty,
                             obs, plan, final_dump, in_liquidation)
            if item == "WHEAT":
                reserve = plan.get("wheat_reserve", 0)
                n = min(n, max(0, qty - reserve))
            if n > 0 and len(market_orders) < 10:
                market_orders.append(["SELL", item, n])

    # -- Seeds for focus crops --
    if not in_liquidation and len(market_orders) < 9:
        for crop in focus_crops:
            seed_cost = CROPS[crop]["seed"]
            have_seeds = seeds.get(crop, 0)
            if have_seeds < 3 and projected_money >= seed_cost + CASH_RESERVE:
                qty = min(3 - have_seeds, int((projected_money - CASH_RESERVE) // seed_cost))
                if qty > 0:
                    market_orders.append(["BUY_SEED", crop, qty])
                    projected_money -= qty * seed_cost
                    break  # one seed purchase per turn keeps orders budget free

    # -- Animal purchase (economic engine decided we want one) --
    if invest_animal and len(market_orders) < 9:
        inventories_list = private.get("inventories", [])
        owned_or_pending = _count_owned_or_pending_animal(invest_animal, me, shed, inventories_list)
        placed_of_this_type = sum(
            1 for row in me["tiles"] for t in row
            if isinstance(t, dict) and t.get("animal") == invest_animal
        )
        unplaced_in_transit = owned_or_pending - placed_of_this_type
        cost = ANIMALS[invest_animal]["cost"]
        structure = ANIMALS[invest_animal]["structure"]
        structure_needed = not any(
            isinstance(t, dict) and t.get("kind") == structure and not t.get("animal")
            for row in me["tiles"] for t in row
        )
        total_investment = cost + (BUILD_COST.get(structure, 0) if structure_needed else 0)
        # Never buy a second one while the last purchase is still sitting
        # unplaced (in the shed or a unit's pocket) — that just means the
        # structure isn't built yet, buying another only wastes cash on an
        # animal that also can't be placed.
        if (
            owned_or_pending < MAX_ANIMALS_V1
            and unplaced_in_transit == 0
            and projected_money >= total_investment + CASH_RESERVE
        ):
            market_orders.append(["BUY_ANIMAL", invest_animal, 1])
            projected_money -= cost

    # -- Fertilizer for premium crops --
    if not in_liquidation and len(market_orders) < 9:
        growing_premium = any(
            isinstance(tile, dict) and tile.get("kind") == "PLANT" and tile["crop"] in PREMIUM_CROPS
            for row in me["tiles"] for tile in row
        )
        if growing_premium and shed.get("FERTILIZER", 0) == 0 and projected_money >= PRODUCT_BASE_PRICE["FERTILIZER"] + CASH_RESERVE:
            market_orders.append(["BUY_PRODUCT", "FERTILIZER", 1])
            projected_money -= PRODUCT_BASE_PRICE["FERTILIZER"]

    # -- Land expansion: only when crowded and enough season left to pay it back --
    quadrants_owned = len(me["unlocked_quadrants"])
    if (
        not in_liquidation
        and quadrants_owned < MAX_LAND_QUADRANTS
        and days_remaining > 8
        and _empty_tile_ratio(me) < 0.15
        and len(market_orders) < 9
        and (plan.get("bottleneck") == "LAND" or plan.get("confidence", 0) > 0.80)
        and plan.get("risk_budget", 0.5) >= 0.35
    ):
        next_cost = LAND_COSTS[min(quadrants_owned - 1, len(LAND_COSTS) - 1)]
        if projected_money >= next_cost + CASH_RESERVE:
            market_orders.append(["BUY_LAND"])
            projected_money -= next_cost

    # -- Hiring: cheap extra hands while there's a real task backlog --
    if not in_liquidation and len(market_orders) < 9:
        hires_today = me.get("hires_today", 0)
        # fib-ish cost sequence: 1,1,2,3,5,8,13,21...
        a, b = 1, 1
        for _ in range(hires_today):
            a, b = b, a + b
        next_hire_cost = a
        backlog = len(targets)
        current_workers = 1 + len(me["hands"])
        if (
            next_hire_cost <= 21  # was 8 — allow scaling labor further to
                                    # actually work the 4th quadrant of land
            and backlog > current_workers * 3
            and plan.get("bottleneck") == "LABOR"
            and hires_today < 12
            and projected_money >= next_hire_cost + CASH_RESERVE
        ):
            market_orders.append(["HIRE"])
            projected_money -= next_hire_cost

    market_orders = market_orders[:10]

    return {"farmer": farmer_action, "hands": hands_actions, "market": market_orders}

# ============================================================================
# V4 COMPETITIVE OVERLAY
# Goal: maximize leaderboard reward, not merely heuristic plausibility.
# This layer adds exact market-curve valuation, adaptive production targets,
# standing-on-work assignment, and global worker matching. It is deliberately
# self-contained and standard-library only.
# ============================================================================

V4_TARGETS = {
    "MAX_HANDS": 12,
    "MAX_QUADRANTS": 3,
    "COW": 8,
    "SHEEP": 6,
    "GOOSE": 4,
}


def _v4_market_price(item, inventory):
    """Approximate the official Kaggriculture dynamic price curve exactly.
    Used only for planning; the observation's quoted price remains authoritative
    for actual orders.
    """
    p = MARKET_PARAMS.get(item)
    if not p:
        return float(PRODUCT_BASE_PRICE.get(item, 1))
    base = float(p["base"])
    i0 = float(p["I0"])
    x = max(0.0, abs(float(inventory) - i0))

    def shape(name, z):
        if name == "linear": return z
        if name == "sq": return z * z
        if name == "sqrt": return math.sqrt(z)
        if name == "log": return math.log(1.0 + z)
        if name == "log10": return math.log10(1.0 + z)
        return z

    if inventory <= i0:
        func, target = p["below_func"], p["below_target"]
    else:
        func, target = p["above_func"], p["above_target"]
    f_t = shape(func, float(p["T"])) or 1.0
    amp = target * base / f_t
    price = base + (1.0 if inventory <= i0 else -1.0) * amp * shape(func, x)
    return max(1.0, price)


def _v4_public_inventory(obs):
    inv = {k: 0 for k in PRODUCT_BASE_PRICE}
    for farm in obs.get("farms", []):
        for row in farm.get("tiles", []):
            for tile in row:
                if not isinstance(tile, dict):
                    continue
                if tile.get("kind") == "PLANT":
                    crop = tile.get("crop")
                    if crop in inv:
                        inv[crop] += int(tile.get("yield_units", 0) or 0)
                animal = tile.get("animal")
                if animal in ANIMALS:
                    product = ANIMALS[animal]["product"]
                    inv[product] += int(tile.get("yield_units", 0) or 0)
    for farm_i, farm in enumerate(obs.get("farms", [])):
        private = obs.get("private", {}) if farm_i == obs.get("player", 0) else {}
        for k, v in (private.get("shed", {}) or {}).items():
            if k in inv:
                inv[k] += int(v or 0)
    return inv


def _v4_count_structures(farm, kind):
    return sum(1 for row in farm.get("tiles", []) for t in row
               if isinstance(t, dict) and t.get("kind") == kind)


def _v4_count_animals(farm):
    out = {a: 0 for a in ANIMALS}
    for row in farm.get("tiles", []):
        for t in row:
            if isinstance(t, dict) and t.get("animal") in out:
                out[t["animal"]] += 1
    return out


def _v4_phase(obs, me):
    step = int(obs.get("step", 0) or 0)
    day = int(obs.get("day", step // 24) or 0)
    if step >= 696:
        return "LIQUIDATE"
    if day <= 4:
        return "BOOTSTRAP"
    if day <= 20:
        return "COMPOUND"
    return "REALIZE"


def _v4_portfolio(obs, plan):
    """Adaptive prior for the high-performing mixed-farm route.
    It is a prior, not a blind fixed script: market pressure, cash, and score
    gap can reduce the target herd/land/hands.
    """
    me_idx = obs["player"]
    me = obs["farms"][me_idx]
    opp = obs["farms"][1 - me_idx]
    phase = _v4_phase(obs, me)
    cash = float(me.get("money", 0))
    gap = cash - float(opp.get("money", 0))
    animals = _v4_count_animals(me)
    quadrants = len(me.get("unlocked_quadrants", []))
    workers = 1 + len(me.get("hands", []))
    inv = _v4_public_inventory(obs)
    prices = obs.get("market", {}).get("prices", {})

    target = dict(V4_TARGETS)
    # Strong lead => lower risk. Strong deficit => pursue compounding capacity.
    if gap > 5000:
        target["MAX_HANDS"] = 9
        target["COW"] = 6
        target["SHEEP"] = 4
    elif gap < -5000:
        target["MAX_HANDS"] = 12
        target["COW"] = 8
        target["SHEEP"] = 6

    # Do not buy long-payback assets during realization/liquidation.
    if phase in ("REALIZE", "LIQUIDATE"):
        target["MAX_HANDS"] = min(target["MAX_HANDS"], workers)
        target["COW"] = min(target["COW"], animals["COW"])
        target["SHEEP"] = min(target["SHEEP"], animals["SHEEP"])
        target["GOOSE"] = min(target["GOOSE"], animals["GOOSE"])

    # Market glut suppresses further production of the glut-sensitive premium.
    if prices.get("MILK", 0) < 90 or inv.get("MILK", 0) > 120:
        target["COW"] = min(target["COW"], animals["COW"] + 2)
    if prices.get("WOOL", 0) < 110 or inv.get("WOOL", 0) > 100:
        target["SHEEP"] = min(target["SHEEP"], animals["SHEEP"] + 2)
    return target


def _v4_job_value(hint, tile, plan, day, days_remaining):
    # Survival work is non-negotiable.  Production work is ranked by the
    # economic route and by how expensive it is to lose one turn.
    if hint == "WATER":
        return 12000.0
    if hint == "FEED":
        return 11800.0
    if hint == "CARE":
        return 10800.0
    if hint in ("HARVEST", "HARVEST_ANIMAL"):
        return 8200.0
    if hint == "FERTILIZE":
        return 6500.0
    if hint == "COLLECT_FERTILIZER":
        return 5200.0
    if hint.startswith("PLACE_ANIMAL"):
        return 5000.0
    if hint.startswith("PLANT_"):
        crop = hint.replace("PLANT_", "")
        if crop == "WHEAT": return 4100.0
        if crop == "STRAWBERRY": return 3900.0
        return 1800.0
    if hint == "DIG":
        return 2600.0
    return 1000.0


def _v4_hungarian(cost):
    """Minimum-cost assignment, rectangular, standard-library only."""
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0]) if cost[0] else 0
    if m == 0:
        return [-1] * n
    transposed = False
    a = cost
    if n > m:
        transposed = True
        a = [list(x) for x in zip(*cost)]
        n, m = m, n
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [float("inf")] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    ans = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            ans[p[j] - 1] = j - 1
    if not transposed:
        return ans
    # Assignment for original rows from transposed solution.
    out = [-1] * len(cost)
    for row_t, col_orig in enumerate(ans):
        if col_orig >= 0 and col_orig < len(out):
            out[col_orig] = row_t
    return out


def _v4_best_task_actions(obs, plan, targets, units, inventories, used_targets):
    """Global worker assignment with resource compatibility and standing work.

    The previous V4 could assign FEED/FERTILIZE to a worker who did not carry
    the required resource; that worker then silently PASSed.  This version
    treats inventory compatibility as a hard constraint and gives shed
    logistics a first-class place in the assignment.
    """
    board = obs["farms"][obs["player"]]["tiles"]
    day = int(obs.get("day", 0) or 0)
    remaining = max(TOTAL_SEASON_DAYS - day, 0)

    def compatible(ui, hint):
        inv = inventories[ui] or {}
        if hint == "FEED":
            return inv.get("WHEAT", 0) > 0
        if hint == "FERTILIZE":
            return inv.get("FERTILIZER", 0) > 0
        if hint.startswith("PLACE_ANIMAL_"):
            structure = hint.replace("PLACE_ANIMAL_", "")
            return any(inv.get(a, 0) > 0 and info["structure"] == structure
                       for a, info in ANIMALS.items())
        return True

    available = []
    for ti, t in enumerate(targets):
        key = (t[1], t[2])
        if key in used_targets or not compatible(0, t[3]) and False:
            continue
        available.append((ti, t))

    assignments = {}
    remaining_units = []
    remaining_tasks = []

    # Standing-on-work pass, but only when the worker can legally perform it.
    for ui, pos in enumerate(units):
        found = None
        for ai, (ti, t) in enumerate(available):
            if (t[1], t[2]) in used_targets:
                continue
            if tuple(pos) == (t[1], t[2]) and compatible(ui, t[3]):
                found = (ai, t)
                break
        if found:
            _, t = found
            used_targets.add((t[1], t[2]))
            assignments[ui] = t
        else:
            remaining_units.append(ui)

    # Global matching for the remaining work.  Incompatible pairs receive a
    # very large cost, effectively removing them from the assignment.
    for _, t in available:
        if (t[1], t[2]) not in used_targets:
            remaining_tasks.append(t)

    if remaining_units and remaining_tasks:
        matrix = []
        for ui in remaining_units:
            pos = units[ui]
            row = []
            for t in remaining_tasks:
                if not compatible(ui, t[3]):
                    row.append(1e9)
                    continue
                priority = float(t[0])
                value = _v4_job_value(t[3], board[t[2]][t[1]], plan, day, remaining)
                distance = _manhattan(pos, (t[1], t[2]))
                # Preserve emergency tiers while strongly rewarding productive
                # actions and short routes.
                row.append(priority * 140.0 + distance * 2.5 - value * 0.01)
            matrix.append(row)
        match = _v4_hungarian(matrix)
        for r, c in enumerate(match):
            if c >= 0 and c < len(remaining_tasks) and matrix[r][c] < 5e8:
                assignments[remaining_units[r]] = remaining_tasks[c]

    return assignments


def _v4_route_action(pos, target):
    tx, ty = target[1], target[2]
    if tuple(pos) == (tx, ty):
        return None
    return [_step_towards(pos, (tx, ty))]


def _v4_upgrade_agent(obs):
    """Optional execution overlay. Returns None when the legacy executor should
    handle the turn, or a complete action dict when V4 has a higher-confidence
    route for the current state.
    """
    player = obs["player"]
    me = obs["farms"][player]
    private = obs["private"]
    day = int(obs.get("day", 0) or 0)
    phase = _v4_phase(obs, me)
    plan = _STATE.get("plan", {}) or {}
    portfolio = _v4_portfolio(obs, plan)

    # V4 is most valuable after bootstrap, when crew assignment dominates.
    # Keep the existing economic layer in charge of purchases and focus crops.
    if phase == "BOOTSTRAP" and day < 2:
        return None

    units = [tuple(me["farmer"])] + [tuple(h) for h in me.get("hands", [])]
    inventories = list(private.get("inventories", []) or [])
    while len(inventories) < len(units):
        inventories.append({})
    _STATE["_wheat_fetch_dispatched_this_turn"] = False

    targets = _find_targets(
        me["tiles"], day, max(TOTAL_SEASON_DAYS - day, 0),
        plan.get("focus_crops", ["WHEAT"]), private.get("seeds", {}),
        plan.get("invest_animal"),
        _count_owned_or_pending_animal(plan.get("invest_animal"), me,
                                       private.get("shed", {}), inventories)
        if plan.get("invest_animal") else 0,
        total_workers=len(units),
    )
    used = set()
    actions = _v4_best_task_actions(obs, plan, targets, units, inventories, used)
    out = {"farmer": ["PASS"], "hands": []}

    for ui, pos in enumerate(units):
        inv = inventories[ui]
        act = None

        # Shed logistics has priority over a newly assigned field task.  This
        # prevents the overlay from undoing the economic executor's feed and
        # produce shuttle logic.
        if _is_shed_adjacent(pos, len(me["tiles"])):
            # WHEAT is deliberately excluded here: it's a sellable product,
            # but a unit that just PICKUP'd it is doing so to feed an animal.
            # Counting it as "produce to drop" created an infinite PICKUP/DROP
            # loop at the shed tile (confirmed by direct trace: the farmer
            # never once left the shed to actually reach the animal, cycling
            # PICKUP-WHEAT then DROP every turn for the animal's entire life).
            carrying_produce = any(
                inv.get(k, 0) > 0 for k in PRODUCT_BASE_PRICE if k not in ("FERTILIZER", "WHEAT")
            )
            carrying_animal = any(inv.get(a, 0) > 0 for a in ANIMALS)
            if carrying_produce and not carrying_animal:
                act = ["DROP"]
            elif carrying_animal:
                # Place it if a matching empty structure exists; otherwise
                # leave it at the shed for the next logistics pass.
                for animal, info in ANIMALS.items():
                    if inv.get(animal, 0) > 0:
                        has_slot = any(
                            isinstance(t, dict) and t.get("kind") == info["structure"] and not t.get("animal")
                            for row in me["tiles"] for t in row
                        )
                        if has_slot:
                            act = [_step_towards(pos, min(
                                [(x, y) for y, row in enumerate(me["tiles"]) for x, t in enumerate(row)
                                 if isinstance(t, dict) and t.get("kind") == info["structure"] and not t.get("animal")],
                                key=lambda q: _manhattan(pos, q)
                            ))] if not any(
                                isinstance(t, dict) and t.get("kind") == info["structure"] and not t.get("animal") and tuple(pos) == (x, y)
                                for y, row in enumerate(me["tiles"]) for x, t in enumerate(row)
                            ) else ["PLACE", animal]
                            break
            elif me.get("money", 0) > 0 and _count_my_animals(me) > 0 and inv.get("WHEAT", 0) == 0 and private.get("shed", {}).get("WHEAT", 0) > 0:
                act = ["PICKUP", "WHEAT", min(4, private["shed"].get("WHEAT", 0))]
            elif inv.get("FERTILIZER", 0) == 0 and private.get("shed", {}).get("FERTILIZER", 0) > 0:
                act = ["PICKUP", "FERTILIZER", 1]
            elif plan.get("invest_animal") and private.get("shed", {}).get(plan["invest_animal"], 0) > 0:
                # V4's shed logic never had a PICKUP for a purchased animal —
                # without this it sits in the shed untouched all season
                # (confirmed via a real Kaggle validation replay).
                act = ["PICKUP", plan["invest_animal"], 1]

        # Hard delivery invariant, independent of position: a unit already
        # carrying an animal must head straight for the nearest empty
        # matching structure, no matter what the Hungarian assignment below
        # wants instead. The shed-adjacent branch above only catches this
        # while the unit happens to be AT the shed — once it walks away
        # carrying the animal it would otherwise take a normal field task
        # and drag the animal along for days (also confirmed via replay).
        if act is None:
            carried_animal = next((a for a in ANIMALS if inv.get(a, 0) > 0), None)
            if carried_animal is not None:
                structure = ANIMALS[carried_animal]["structure"]
                slots = [
                    (x, y) for y, row in enumerate(me["tiles"]) for x, t in enumerate(row)
                    if isinstance(t, dict) and t.get("kind") == structure and not t.get("animal")
                ]
                if slots:
                    slot = min(slots, key=lambda q: _manhattan(pos, q))
                    act = ["PLACE", carried_animal] if tuple(pos) == slot else _v4_route_action(pos, (0, slot[0], slot[1], "PLACE"))
                else:
                    nearest_shed = min(_shed_pos_candidates(len(me["tiles"])), key=lambda s: _manhattan(pos, s))
                    act = ["DROP"] if tuple(pos) == nearest_shed else [_step_towards(pos, nearest_shed)]

        # An animal about to escape tonight needs FEED, but FEED is only
        # offered to a unit already carrying wheat — an empty-handed unit
        # would otherwise skip straight past this target to normal field
        # work and the animal starves (confirmed: cows died by day 16 even
        # with FEED at the top priority tier, because nobody happened to be
        # holding wheat at the critical moment). Send one empty-handed unit
        # to fetch wheat right now, ahead of everything else.
        if act is None and inv.get("WHEAT", 0) <= 0 and not _STATE.get("_wheat_fetch_dispatched_this_turn", False):
            urgent_feed_pending = any(t[3] == "FEED" and t[0] == -1 for t in targets)
            if urgent_feed_pending and private.get("shed", {}).get("WHEAT", 0) > 0:
                if _is_shed_adjacent(pos, len(me["tiles"])):
                    act = ["PICKUP", "WHEAT", min(5, private["shed"].get("WHEAT", 0))]
                else:
                    nearest_shed = min(_shed_pos_candidates(len(me["tiles"])), key=lambda s: _manhattan(pos, s))
                    act = [_step_towards(pos, nearest_shed)]
                _STATE["_wheat_fetch_dispatched_this_turn"] = True

        target = actions.get(ui)
        if act is None and target is not None:
            hint = target[3]
            if tuple(pos) == (target[1], target[2]):
                act = _act_on_tile(hint, inv)
            else:
                act = _v4_route_action(pos, target)
        if act is None:
            act = ["PASS"]
        if ui == 0:
            out["farmer"] = act
        else:
            out["hands"].append(act)

    # Preserve the legacy market engine: it already handles reserves, buys,
    # liquidation, and the ten-order cap. The V4 overlay only changes worker
    # execution, avoiding a second independent market policy fighting it.
    return out


# Wrap the original agent so V4 can take control of worker routing without
# rewriting the proven economic/market layer. If anything unexpected occurs,
# the original policy remains the safe fallback.
_V3_AGENT = agent


def agent(obs):
    try:
        base = _V3_AGENT(obs)
        upgraded = _v4_upgrade_agent(obs)
        if upgraded is None:
            return base
        # Keep market orders from the proven V3 economic engine.
        upgraded["market"] = base.get("market", [])
        return upgraded
    except Exception:
        return _V3_AGENT(obs)
