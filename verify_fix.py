from veilbound.shared.engine import GameEngine
from veilbound.shared.models import GameState, PlayerState, BattleRow, CardInstance
from veilbound.shared.abilities import AbilityManager

e = GameEngine()
m = AbilityManager(e.cards)
cond = [c for c in e.cards["naruto_02"].ability.conditions if c.type == "same_row"][0]


def check(p1_owner, p2_owner, label):
    st = GameState(
        players=[PlayerState("p1", "P1"), PlayerState("p2", "P2")],
        rows=[BattleRow(id="r0", number=1, capacity=2, slots=[None, None])],
        rounds_per_match=2,
    )
    st.rows[0].slots = [
        CardInstance("s1", "naruto_02", p1_owner, revealed=True, current_points=7),
        CardInstance("i1", "naruto_03", p2_owner, revealed=True, current_points=6),
    ]
    result = m._condition_holds(cond, st.rows[0].slots[0], st.rows[0], st)
    print(f"{label}: {result}")


check("p1", "p1", "INTENDED same-player (expect True)")
check("p1", "p2", "OPPONENT Itachi (expect False)")
check("p2", "p1", "cross-owner (expect False)")
print("Sasuke base:", e.cards["naruto_02"].base_points)
print("Sasuke ability id:", e.cards["naruto_02"].ability.id)
