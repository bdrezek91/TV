# Proposed update to the existing Claude routine

**STATUS: PROPOSED ONLY — NOT APPLIED.**

This session has **no access to, and made no attempt to call, any
routine/scheduled-trigger management tool.** The ~2-hour market-scan
routine referenced in the plan is a scheduling object that lives outside
this git repository, in a separate orchestration system this sandbox
cannot reach. Nothing about it was read, listed, modified, or touched.

This file exists purely so the orchestrating session/user has the exact
proposed prompt text in hand, per the plan's "Aktualizacja istniejącej
rutyny Claude" section. **Do not apply this until the Block 1-3 code in
this repository has actually been deployed to the real VPS and the new MCP
tools (`trading_system_health`, `run_futures_opportunity_scan`, etc.) are
live on the connector** — pointing the routine at tools that don't exist
yet on the deployed server would break it.

## Before applying (when the user is ready, on the real VPS)

1. Save the routine's current prompt text somewhere safe (a backup).
2. Confirm the new MCP tools are visible to the connector (call
   `trading_system_health` once manually and confirm it responds instead
   of "tool not found").
3. Confirm `docker compose ps` shows `bybit-collector` /
   `analysis-worker` / `postgres` / `redis` all healthy, and that
   `trading_system_health` reports `overall_status: HEALTHY` (or close to
   it) rather than `DATABASE_DOWN` / `CONFIG_INVALID`.
4. Only then replace the routine's prompt with the text below, keeping the
   existing ~2-hour schedule unless the user explicitly wants it changed.
5. Diff the old vs. new prompt text for the user to review before
   confirming the change.

## Proposed routine prompt (verbatim from the plan)

```text
Przeanalizuj aktualne okazje wyłącznie na rynku Bybit linear futures.

Najpierw użyj narzędzia trading_system_health.

Jeżeli dane są nieświeże, order book jest niespójny, data_quality nie wynosi GOOD albo krytyczny komponent nie działa, nie podawaj setupu. Zwróć ostrzeżenie techniczne.

Następnie użyj run_futures_opportunity_scan z:
- minimum_score=75,
- maximum_results=2.

Nie wymyślaj brakujących wartości.

Nie przeliczaj samodzielnie:
- wejścia,
- stop lossa,
- take profitów,
- score,
- wielkości pozycji.

Użyj wyłącznie wartości zwróconych przez system.

Dla każdego setupu pokaż:
- instrument,
- kierunek,
- nazwę setupu,
- reżim rynku,
- score,
- najważniejsze składniki score,
- strefę wejścia,
- stop loss,
- TP1,
- TP2,
- RR,
- czas wygaśnięcia,
- warunek invalidacji,
- najważniejsze zagrożenia.

Jeżeli status wynosi NO_TRADE, napisz jednoznacznie, że obecnie nie ma setupu spełniającego wymagania.

Nie szukaj na siłę alternatyw.
Nie obniżaj progu.
Nie zmieniaj reguł strategii.
Nie wykonuj transakcji.
```

## Why this is safe to defer

Every constraint in the routine text above (never invent values, never
recompute entry/SL/TP/score, never execute trades, always report NO_TRADE
honestly) is already enforced at the code level, not just in the prompt:

- `run_futures_opportunity_scan` (Block 3) only ever returns values
  already computed and persisted by the Strategy/Risk Engine — it cannot
  compute a new number itself.
- There is no order-placement code path anywhere in this project (grepped
  and confirmed in `docs/trading-system/BLOCK3_REPORT.md` §"no live
  trading" verification).
- `TRADING_MODE` is validated to be exactly `"paper"` at process startup;
  the system refuses to start otherwise.

So applying this prompt update later is a low-risk, mechanical step once
the underlying tools exist on the live connector — there's no reason to
rush it ahead of that, and real reason (a routine calling nonexistent
tools) not to.
