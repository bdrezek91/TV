# Warstwa 1-5 (Research Pipeline) — Raport stanu i weryfikacji

> **UWAGA: V14 jest w fazie POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION – badania nad nowymi strategiami. Nie jest przeznaczony do walidacji produkcyjnej.**

Data ostatniej aktualizacji: 2026-08-12
Branch: `main` (repo: `bdrezek91/TV`)
VPS: `~/tradingview-mcp` na `server750497`
Historyczne commity opisywanego etapu: `e865867` (Warstwa 5), `b028828` (3 bugfixy), `9dccdbb` (trwały wolumen)

---

## 0. Ten dokument w skrócie

To jest **drugi, nowszy system** obok tego opisanego w `PROJECT_SUMMARY.md`
(Blok 1-3: `bybit-collector` → `analysis-worker` → stare narzędzia MCP typu
`run_futures_opportunity_scan`). Oba działają **w tym samym repo, na tym
samym VPS, współdzielą tę samą bazę danych** (świece, order flow, likwidacje
zbiera dalej `bybit-collector`), ale mają **osobną, nowszą logikę decyzyjną**
("Warstwa 1-5" / `*_v2` moduły) i **osobny zestaw 6 narzędzi MCP**
(`research_*`). Stary system nie został usunięty — działa równolegle.

**Jeśli chcesz wiedzieć "co realnie steruje sygnałami, które teraz widzę w
Routine 'Skan rynku'" — to jest ten system, nie Blok 1-3.**

---

## 1. Po co ten system istnieje

Cel: zamienić "Claude liczy wskaźniki na żywo i sam decyduje o wejściu" na
**deterministyczny, testowalny, w pełni odtwarzalny łańcuch decyzyjny w
Pythonie**, który Claude (przez Routine) tylko odczytuje. Każda decyzja —
także odrzucenie — ma zapisany, jawny powód. Nic nie jest "czarną skrzynką"
w głowie modelu.

Kluczowa zasada nadrzędna: **`TRADING_MODE=paper` na stałe, zero kodu
składającego realne zlecenia**. To wciąż jest silnik badawczy/symulacyjny,
nie bot handlowy.

---

## 2. Architektura — 5 warstw

```
Bybit (WebSocket + REST) ──► bybit-collector ──► PostgreSQL
                                                       │
                    ┌──────────────────────────────────┘
                    ▼
        WARSTWA 1 — Regime Classifier (regime_classifier_v2.py)
        Klasyfikuje rynek: TREND_UP/DOWN, RANGE, BREAKOUT_UP/DOWN,
        SQUEEZE, PANIC_LIQUIDATION_LONGS/SHORTS, NO_EDGE, UNSTABLE_DATA.
        Histereza (nie przeskakuje na każdą świecę), gate jakości danych.
                    │
                    ▼
        WARSTWA 2 — Setup Detection (wewnątrz signal_pipeline_v2.py)
        4 typy: trend_pullback, breakout, mean_reversion,
        liquidation_reversal (ostatni świadomie ograniczony).
        Każdy setup dopasowany do konkretnych reżimów; liczy
        entry_zone / stop_loss / targets / confidence.
                    │
                    ▼
        WARSTWA 3 — Order-Flow Confirmation (orderflow_confirmation_v2.py)
        11 niezależnych wymiarów (CVD, taker buy/sell, OI, funding,
        long/short ratio, likwidacje, orderbook imbalance, spread/depth,
        microprice, large trades, zgodność z reżimem BTC).
        Wynik: CONFIRMED / WEAK_CONFIRMATION / NEUTRAL / CONTRADICTED /
        INSUFFICIENT_DATA. Pilnuje, żeby nie liczyć tego samego dowodu
        dwa razy (dimensions "already_used" przez Warstwę 1/2).
                    │
                    ▼
        WARSTWA 4 — Risk Manager (risk_manager_v2.py)
        R:R gate (min. 1.5 na TP1, nie poszerza SL na siłę), position
        sizing na bazie NAJGORSZEGO możliwego entry w strefie (nie
        środka strefy), limity portfela (total/symbol/grupa
        skorelowana/kierunkowa koncentracja), 5 decyzji: APPROVED /
        APPROVED_REDUCED_SIZE / REJECTED / WAIT_FOR_CONFIRMATION / brak.
                    │
                    ▼
        WARSTWA 5 — Paper Execution (paper_execution_v2.py +
                     paper_broker_service_v2.py)
        Symuluje pełny cykl życia zlecenia na historycznych/przyrostowych
        świecach 1m, BEZ zaglądania w przyszłość: SIGNAL_CREATED →
        PENDING_ENTRY → (PARTIALLY_FILLED) → OPEN → TP1/2/3_HIT /
        STOPPED_OUT → CLOSED, plus CANCELLED / EXPIRED. Trwały stan
        portfela (balance, equity, drawdown, otwarte pozycje) w
        /app/artifacts/research/paper_trading/portfolio_state.json.
```

Cały łańcuch (1→5) dla jednego symbolu = `signal_pipeline_v2.run_pipeline_for_symbol()`.
Dla całego rynku = pętla po 15 parach w `research_query_service.py`.

---

## 3. Narzędzia MCP wystawione connectorowi

Dodane w `server.py`, dostępne dla Routine przez `TV-connector`
(`https://tvscanai.pl/mcp`):

| Narzędzie | Co robi | Zapisuje coś? |
|---|---|---|
| `research_regime_snapshot(symbol)` | Sama Warstwa 1 dla jednego symbolu | Nie |
| `research_trading_chain_snapshot(symbol)` | Pełny łańcuch 1→4 dla jednego symbolu, portfel liczony jako PUSTY (kontekstowo, nie realnie) | Nie |
| `research_market_scan(symbols?)` | Łańcuch 1→4 dla wszystkich (lub wybranych) symboli, skrócony wynik | Nie |
| `research_paper_account_status()` | Odczyt aktualnego, trwałego stanu portfela paper | Odświeża `peak_equity`/`max_drawdown_pct` |
| `research_advance_paper_trading()` | **Jedyne narzędzie z efektem ubocznym.** Odpala pełny łańcuch 1→5 dla wszystkich symboli, tworzy nowe zlecenia dla APPROVED, przesuwa istniejące zlecenia po świecach | Tak — trwały stan portfela + log sygnałów |
| `research_recent_signals(symbol?, limit)` | Ostatnie zapisane sygnały z pełnym łańcuchem i (jeśli już policzony) wynikiem | Nie |

Routine "Skan rynku" w praktyce woła głównie `research_advance_paper_trading`
(żeby faktycznie przesunąć symulację) i/lub `research_market_scan` (do
raportu).

---

## 4. Dyscyplina "no-look-ahead" (dlaczego to nie jest oszukany backtest)

Cała Warstwa 5 (i backtest historyczny Warstwy 1) trzyma się jednej zasady:
**decyzja w czasie `T` może użyć tylko danych dostępnych do `T`, nigdy
później**. Konkretnie:

- Symulacja wypełnień/SL/TP idzie świeca po świecy 1m, sprawdzając tylko
  `open/high/low/close` tej jednej świecy na raz — nigdy nie "podgląda" czy
  cena później wróci.
- Przypadek `AMBIGUOUS_INTRABAR` (SL i TP dotknięte w tej samej świecy, nie
  wiadomo co było pierwsze) rozstrzygany **zawsze na niekorzyść** (konserwatywnie:
  zakłada się SL, nie TP) — nigdy nie zgaduje na korzyść wyniku.
- `PENDING_ENTRY` ma TTL (wygasa) i teraz (od `b028828`) **re-walidację
  tezy**: jeśli order flow, na którym zlecenie zostało oparte, w
  międzyczasie odwróci się w `CONTRADICTED`, zlecenie jest anulowane
  zanim w ogóle dojdzie do wypełnienia — nie czeka biernie na SL.

---

## 5. Historia błędów znalezionych i naprawionych (ten pipeline, ostatnie dni)

1. **Trailing stop no-op** — formuła liczyła `x * mult / mult`, czyli
   mnożnik był ignorowany. Naprawione przed pierwszym wdrożeniem.
2. **`compute_account_summary` mieszał skumulowany unrealized PnL między
   pozycjami** zamiast liczyć PnL każdej osobno; `peak_equity`/`max_drawdown_pct`
   nie były zapisywane z powrotem do stanu. Naprawione przed wdrożeniem.
3. **Kolizja zapisu snapshotów** — Warstwa 3 (`audit_orderflow_confirmation.py`)
   i Warstwa 4/5 obie próbowały zapisywać ten sam `signal_id` — usunięto
   zapis z Warstwy 3, wyłączna odpowiedzialność przeniesiona do Warstwy 4/5.
4. **Błąd znaku funding rate** (`regime_classifier_v2._reasons_for_funding`) —
   dodatni funding przy LONG (crowding **za** pozycją, ryzyko squeeze) był
   błędnie liczony jako argument NEUTRALNY/korzystny zamiast kontrargumentu,
   i odwrotnie dla SHORT. Znaleziony przez niezależną weryfikację
   użytkownika na żywych danych, potwierdzony matematycznie, naprawiony.
5. **Sizing ryzyka liczony od środka strefy wejścia**, nie od najgorszej
   ceny możliwej w tej strefie — realne ryzyko mogło przekroczyć zadeklarowane
   `risk_amount` o 30-50% w zależności od tego, gdzie w strefie nastąpi
   wypełnienie. Naprawione: sizing teraz liczony od `worst_case_entry`
   (krawędź strefy najdalsza od SL), `reference_price` (środek) zostaje
   tylko jako pole kontekstowe/wyświetlane.
6. **Brak re-walidacji tezy dla `PENDING_ENTRY`** — zlecenie oczekujące na
   wypełnienie nie sprawdzało, czy uzasadnienie order-flow, na którym
   powstało, wciąż jest aktualne. Potwierdzone na żywo: LTCUSDT LONG
   zmienił status z `WEAK_CONFIRMATION` na `CONTRADICTED` w 12 minut, a
   zlecenie dalej czekało na wypełnienie zamiast się anulować. Naprawione
   funkcją `reevaluate_thesis()` — anuluje zlecenie **tylko** przy twardym
   zwrocie w `CONTRADICTED`, nigdy przy słabszych wahaniach, nigdy po
   wypełnieniu.
7. **Brak trwałego wolumenu Docker pod `/app/artifacts`** — cały stan
   portfela paper i log sygnałów żyły tylko w warstwie zapisu kontenera,
   więc każdy `docker compose build`/`--force-recreate` zerował konto do
   `balance=10000, orders={}`. Właśnie tak zniknęły pierwsze realne
   zlecenia LTCUSDT/SUIUSDT wygenerowane przez Routine. Naprawione
   (`9dccdbb`): named volume `trading-research-artifacts:/app/artifacts`,
   **zweryfikowane empirycznie** — stan przeżył pełny `build --no-cache` +
   `force-recreate` na VPS (ten sam `created_at` przed i po).

## 6. Infrastruktura connectora (osobna, ale krytyczna sprawa)

Domena `tvscanai.pl` dzieli reverse proxy Caddy z **innym, niepowiązanym**
projektem "Multiplekser" (inna domena, inny cel — świadomie nietknięty poza
jego wspólnym Caddyfile). Znalezione i naprawione:

- **Docker network split** — `tradingview-mcp` i Caddy były na różnych
  sieciach Compose, więc `reverse_proxy tradingview-mcp:8000` nie mógł
  rozwiązać nazwy → 502. Naprawione: `trading-net` zadeklarowana jako
  `external: true` w obu compose files, żeby przetrwać kolejne `down`/recreate.
- **Zły endpoint** — connector pytał `https://tvscanai.pl/` zamiast
  `.../mcp` (domyślna ścieżka FastMCP).
- **421 Invalid Host header** — biblioteka `mcp` wymaga portu w nagłówku
  `Host` (`allowed_hosts` dopasowuje `localhost:*`), a Caddy wysyłał `Host:
  localhost` bez portu. Naprawione: `header_up Host localhost:8000`.

Stan: **potwierdzony end-to-end** — Routine "Skan rynku" realnie woła
connector, connector realnie dociera do kontenera, kontener realnie czyta
bazę i zwraca żywe dane (potwierdzone kilkoma pełnymi raportami z Routine).

---

## 7. Co jest ZWERYFIKOWANE na dziś (2026-08-10)

- Warstwa 1: **14/14** testów syntetycznych + 2 regresje PASS
- Warstwa 4: **16/16** testów syntetycznych PASS
- Warstwa 5: **19/19** testów syntetycznych PASS (w tym nowy test
  re-walidacji tezy)
- Live run na 15 realnych parach, na żywych danych VPS, po fixach z
  `b028828`: łańcuch 1→5 działa bez wyjątków, decyzje spójne (R:R gate i
  order-flow gate działają niezależnie, tak jak powinny)
- Trwałość stanu portfela paper przez rebuild kontenera — potwierdzona
  eksperymentalnie
- Connector ↔ VPS ↔ Routine — potwierdzony kilkoma realnymi raportami

---

## 8. Co NIE jest jeszcze zweryfikowane / ograniczenia

To jest najważniejsza sekcja, jeśli pytanie brzmi "czy to będzie działać":

- **Zero zamkniętych transakcji paper do dziś.** System dopiero zaczął
  realnie zbierać zlecenia (poprzednia partia przepadła przez brak
  wolumenu — patrz punkt 5.7). Nie ma jeszcze żadnej próby statystycznej —
  win rate, profit factor, expectancy są dosłownie niepoliczalne (0 prób).
  **"Czy strategia jest zyskowna" jest dziś pytaniem bez odpowiedzi, nie
  odpowiedzią twierdzącą ani przeczącą.**
- Rynek od kilku dni jest w niskiej zmienności (głównie RANGE/NO_EDGE na
  15 parach) — system to poprawnie odrzuca, ale to też oznacza, że
  jeszcze nie widzieliśmy go w akcji na realnym trendzie/breakout/panice.
- Dashboard Grafany nie zweryfikowany wizualnie dla tego pipeline'u.
- Warianty money-management (0.5%/1%/1.5%/2% ryzyka) z sekcji 11
  tygodniowego raportu — nie ma jeszcze równoległych symulacji, tylko
  jeden tor na `RISK_PER_TRADE_PCT`.
- Fixy z `b028828` (funding sign, worst-case sizing, re-walidacja tezy)
  są zweryfikowane **testami jednostkowymi**, ale nie ma jeszcze
  wielodniowego realnego przebiegu, który by je przetestował na
  różnorodnym zestawie sygnałów.
- Routine "Analiza tygodniowa" (osobna sprawa, opisana w rozmowie) miała
  pętlę retry przy zerowej próbie danych — poprawka gotowa, ale wymaga
  ręcznego wklejenia w UI Claude.ai (nie mam programistycznego dostępu do
  tej konkretnej Routine).

---

## 9. Więc — czy to będzie działać?

**Mechanicznie: tak, i to już potwierdzone.** Cały łańcuch decyzyjny
(regime → setup → order-flow → risk → paper execution) jest deterministyczny,
przetestowany (49 testów syntetycznych łącznie), działa na żywych danych bez
błędów, poprawnie odrzuca słabe sygnały, a stan teraz przetrwa restarty.

**Jako strategia inwestycyjna: za wcześnie żeby powiedzieć.** Zero
zamkniętych transakcji to zero dowodu na przewagę rynkową w jakąkolwiek
stronę. To normalne na tym etapie — sensowna odpowiedź przyjdzie dopiero po
tygodniach realnego paper tradingu na różnych warunkach rynkowych (stąd
sens Routine "Analiza tygodniowa" — ale ta też potrzebuje realnej próby,
nie tylko poprawnie działającego promptu).

**Co obserwować, żeby to samemu zweryfikować:**
1. `research_paper_account_status()` — czy liczba `active_orders`/
   `open_positions` rośnie w czasie (system faktycznie coś robi, nie stoi
   w miejscu)
2. Po pierwszych zamkniętych pozycjach — czy `outcome` w
   `research_recent_signals()` jest wypełniony i sensowny (cena
   faktycznie dotarła tam, gdzie system twierdzi)
3. Za ok. 7 dni — pierwszy sensowny raport tygodniowy (przy >5 zamkniętych
   pozycjach) zamiast automatycznego "ZA MAŁO DANYCH"
4. Czy `docker compose build --no-cache` + recreate nadal zachowuje stan
   (`created_at` w `portfolio_state.json` się nie zmienia) — jeśli kiedyś
   się zmieni, to sygnał że coś naruszyło wolumen

---

## 10. Jak wdrażać zmiany na VPS (skrót)

```bash
cd ~/tradingview-mcp
git fetch mywork
git reset --hard mywork/main   # albo git pull mywork main --no-rebase, jeśli VPS ma lokalne commity
docker compose build --no-cache tradingview-mcp
docker compose up -d --force-recreate tradingview-mcp
```

Po zmianach w `docker-compose.yml` (np. wolumeny, sieci) — zawsze `up -d`
bez `--no-cache` też wystarczy, jeśli obraz się nie zmienił, ale bezpieczniej
zrobić oba kroki razem.

**Testy audytowe (bezpieczne, nic nie psują, tylko czytają/symulują):**
```bash
docker compose exec tradingview-mcp python scripts/research/audit_regime_classifier.py
docker compose exec tradingview-mcp python scripts/research/audit_risk_manager.py
docker compose exec tradingview-mcp python scripts/research/audit_paper_trading.py
```
