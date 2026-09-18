import copy
import json
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Delivery, MonitorJob, SourceBudget, SourceProbe
from backend.tests.test_conservative_valuation import priced_sample
from backend.tests.test_ria_search import engine
from backend.valuation import estimate
from backend.valuation_audit import check_once, validate_run_id


def retained():
    candidate, peers = priced_sample([900, 1300, 1600, 1800, 6000], asking=950)
    result = {"candidate": candidate, "rating": estimate(candidate, peers),
              "filters": {"private-filter": "private-subscription"}}
    candidate["VIN"], candidate["description"] = "private-vin", "private-seller-text"
    return result


def test_audit_replays_retained_time_once_without_changing_jobs_deliveries_or_budget(engine, caplog):
    result = retained()
    with Session(engine) as db:
        db.add(MonitorJob(source_id="123", state="informational", first_seen=time.time(),
                          last_attempt=time.time(), result=result))
        db.add_all([Delivery(user_id=111, listing_id=1, state="sent"),
                    Delivery(user_id=111, listing_id=2, state="uncertain")])
        db.commit()
        budget = copy.deepcopy(db.get(SourceBudget, "auto_ria").calls)
    check_once(engine, "regression")
    check_once(engine, "regression")
    with Session(engine) as db:
        probe = db.get(SourceProbe, "valuation-audit-regression")
        assert probe.requests == 0 and probe.result["provider_requests"] == 0
        row = probe.result["rows"][0]
        assert row["replay_only"] and row["revised_market_usd"] == 1300
        assert row["original_evaluated_at"] == result["rating"]["valuation_evidence"]["evaluated_at"]
        assert db.get(MonitorJob, "123").result == result
        assert db.get(MonitorJob, "123").state == "informational"
        assert list(db.scalars(select(Delivery.state).order_by(Delivery.id))) == ["sent", "uncertain"]
        assert db.get(SourceBudget, "auto_ria").calls == budget
        assert db.get(SourceBudget, "auto_ria").total == 2
        output = json.dumps(probe.result) + caplog.text
        for private in ("private-vin", "private-seller-text", "private-filter", "private-subscription", "user_id"):
            assert private not in output
    assert caplog.text.count("Valuation evidence replay") == 1


def test_audit_has_a_hard_row_and_age_bound_and_default_does_nothing(engine):
    check_once(engine, "")
    with Session(engine) as db:
        assert list(db.scalars(select(SourceProbe))) == []
        for i in range(22):
            db.add(MonitorJob(source_id=str(1000+i), state="informational", first_seen=time.time(),
                              last_attempt=time.time()+i, result=retained()))
        db.add(MonitorJob(source_id="old", state="informational", first_seen=time.time()-90000,
                          last_attempt=time.time()+100, result=retained()))
        db.commit()
    check_once(engine, "bounded")
    with Session(engine) as db:
        assert len(db.get(SourceProbe, "valuation-audit-bounded").result["rows"]) == 20


@pytest.mark.parametrize("run_id", ["../", "invalid\n", "x" * 41, "secret=value"])
def test_audit_id_validation(run_id):
    with pytest.raises(ValueError):
        validate_run_id(run_id)
