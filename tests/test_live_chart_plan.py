import pytest

from trading_os.live_chart_plan import LiveChartPlanError, derive_live_chart_plan

REF = "artifact:" + "a" * 64 + ".png"


def _chart(entry, stop, target, provenance="chart_annotation"):
    return {
        name: {"value": value, "provenance": provenance, "evidence_ref": REF}
        for name, value in (("entry", entry), ("stop", stop), ("target", target))
    }


def test_long_one_r_is_chart_entry_to_stop_and_rr_comes_from_the_chart():
    plan = derive_live_chart_plan(_chart(100, 98, 106), direction="long")
    assert plan["risk_distance"] == 2.0
    assert plan["planned_rr"] == 3.0
    assert plan["r_basis"] == "actual_entry_to_chart_stop"
    assert "realized_r" not in plan  # open trade: no R yet


def test_short_realized_r_uses_provider_prices_measured_in_chart_risk():
    plan = derive_live_chart_plan(
        _chart(77400, 78730, 72000), direction="short",
        provider_entry_price="77401.4", provider_exit_price="78053.6",
    )
    # 1R is the actual fill (77401.4) to the chart stop (78730) = 1328.6; the
    # chart's own entry (77400) is not used once there is a fill.
    assert plan["risk_distance"] == pytest.approx(1328.6)
    assert plan["realized_r"] == pytest.approx(-0.491, abs=1e-9)
    assert plan["realized_r_authority"] == "deterministic_execution_calculation"


def test_risk_size_does_not_depend_on_position_size_or_a_fixed_amount():
    small = derive_live_chart_plan(_chart(100, 99, 102), direction="long",
                                   provider_entry_price=100, provider_exit_price=99)
    large = derive_live_chart_plan(_chart(100, 90, 120), direction="long",
                                   provider_entry_price=100, provider_exit_price=90)
    assert small["realized_r"] == large["realized_r"] == -1.0


@pytest.mark.parametrize("entry,stop,target", [(100, 102, 106), (100, 98, 99), (100, 100, 105)])
def test_levels_that_contradict_the_direction_are_rejected(entry, stop, target):
    with pytest.raises(LiveChartPlanError, match="contradict"):
        derive_live_chart_plan(_chart(entry, stop, target), direction="long")


def test_missing_provenance_or_unattached_image_is_rejected():
    chart = _chart(100, 98, 106)
    chart["stop"]["provenance"] = "unknown"
    with pytest.raises(LiveChartPlanError, match="provenance"):
        derive_live_chart_plan(chart, direction="long")
    with pytest.raises(LiveChartPlanError, match="not an attached image"):
        derive_live_chart_plan(_chart(100, 98, 106), direction="long", attached_refs={"artifact:other.png"})


def test_realized_r_requires_provider_entry_and_rejects_bad_prices():
    with pytest.raises(LiveChartPlanError, match="provider entry"):
        derive_live_chart_plan(_chart(100, 98, 106), direction="long", provider_exit_price=99)
    with pytest.raises(LiveChartPlanError, match="positive"):
        derive_live_chart_plan(_chart(100, float("nan"), 106), direction="long")
    with pytest.raises(LiveChartPlanError, match="positive"):
        derive_live_chart_plan(_chart(100, 98, True), direction="long")


def test_actual_fill_not_chart_entry_sets_one_r_and_rr_once_filled():
    # Chart entry 100 but the fill was 100.5: risk 2.5 to the 98 stop, reward 5.5.
    plan = derive_live_chart_plan(_chart(100, 98, 106), direction="long",
                                  provider_entry_price="100.5", provider_exit_price="98")
    assert plan["risk_distance"] == 2.5
    assert plan["planned_rr"] == 2.2
    assert plan["realized_r"] == -1.0
    # A fill that is already through the chart stop has no valid risk.
    with pytest.raises(LiveChartPlanError, match="contradict"):
        derive_live_chart_plan(_chart(100, 98, 106), direction="long", provider_entry_price=97)


# --- app_drawn (Allen, 2026-11-23): additive; screenshot provenance above is unchanged ---
def _drawn(entry, stop, target, ref="plan:1700000000-BTC-USDT"):
    c = _chart(entry, stop, target, provenance="app_drawn")
    for v in c.values():
        v["evidence_ref"] = ref
    c["plan_created_at"] = 1700000000
    return c


def test_app_drawn_plan_saved_before_entry_gives_same_r_as_a_screenshot():
    drawn = derive_live_chart_plan(_drawn(100, 98, 106), direction="long",
                                   provider_entry_price=100, provider_exit_price=104,
                                   provider_entry_time=1700000500)
    shot = derive_live_chart_plan(_chart(100, 98, 106), direction="long",
                                  provider_entry_price=100, provider_exit_price=104)
    assert drawn["planned_rr"] == shot["planned_rr"] == 3.0
    assert drawn["realized_r"] == shot["realized_r"] == 2.0
    assert drawn["chart_price_evidence"]["stop"]["provenance"] == "app_drawn"


@pytest.mark.parametrize("created,entry_t", [(1700000500, 1700000500), (1700000900, 1700000500), (1700000000, None)])
def test_app_drawn_rejected_unless_provably_before_entry(created, entry_t):
    chart = _drawn(100, 98, 106)
    chart["plan_created_at"] = created
    with pytest.raises(LiveChartPlanError):
        derive_live_chart_plan(chart, direction="long", provider_entry_time=entry_t)


def test_app_drawn_needs_a_plan_ref_and_screenshots_still_need_attached_images():
    with pytest.raises(LiveChartPlanError):
        derive_live_chart_plan(_drawn(100, 98, 106, ref=REF), direction="long", provider_entry_time=1700000500)
    with pytest.raises(LiveChartPlanError):  # old rule intact: unattached image ref still rejected
        derive_live_chart_plan(_chart(100, 98, 106), direction="long", attached_refs={"other"})
