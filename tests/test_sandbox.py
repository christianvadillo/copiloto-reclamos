"""El sandbox: reclamos nuevos, comprador que escala, ML que resuelve y el copiloto que aprende."""

from fastapi.testclient import TestClient

from copiloto.config import Settings
from copiloto.sandbox import build_sandbox


def _sandbox(tmp_path):
    sb = build_sandbox(Settings(), db_path=str(tmp_path / "sb.db"))
    sb.worker.run_until_idle()
    return sb, TestClient(sb.app)


def test_seeded_claims_get_recommendations(tmp_path):
    sb, client = _sandbox(tmp_path)
    assert client.get("/sandbox").status_code == 200
    for claim_id in ("1001", "2001", "4001"):
        assert sb.store.get_latest_recommendation(claim_id) is not None


def test_new_claim_from_template(tmp_path):
    sb, client = _sandbox(tmp_path)
    resp = client.post("/sandbox/nuevo", data={"plantilla": "2001"}, follow_redirects=False)
    assert resp.status_code == 303
    sb.worker.run_until_idle()
    new_id = max(sb.state.claims, key=int)
    assert new_id != "2001"
    assert sb.store.get_claim_row(new_id)["category"] == "defectuoso"


def test_escalation_then_ruling_is_learned(tmp_path):
    sb, client = _sandbox(tmp_path)
    client.post("/sandbox/evento/1001", data={"accion": "escalar"})
    sb.worker.run_until_idle()
    assert sb.store.get_claim_row("1001")["stage"] == "dispute"
    assert sb.store.get_latest_recommendation("1001")["action"] == "defend"

    # El vendedor defiende ante el mediador (aprobación en el panel) y ML le da la razón.
    draft = sb.store.get_latest_draft("1001")
    client.post("/claims/1001/approve", data={"message": draft["message"]})
    sb.worker.run_until_idle()
    client.post("/sandbox/evento/1001", data={"accion": "favor_vendedor"})
    sb.worker.run_until_idle()
    outcomes = sb.store.outcomes_for_seller(sb.state.seller_id)
    assert len(outcomes) == 1
    assert outcomes[0].escalated is True and outcomes[0].mediation_won is True
