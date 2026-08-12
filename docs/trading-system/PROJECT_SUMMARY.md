# Bybit Trading System — Podsumowanie projektu

> **UWAGA: V3–V14 zostały zamknięte bez promocji. V15 zakończyła development
> wynikiem `DEVELOPMENT_FAIL` i również została zamknięta bez promocji oraz bez
> holdoutu. Żadna z tych wersji nie jest przeznaczona do walidacji produkcyjnej
> ani handlu live. Zobacz `V3_V14_POSTMORTEM.md`, `V15_PREREGISTRATION.md` i
> `V15_DEVELOPMENT_POSTMORTEM.md`.**

Data ostatniej aktualizacji: 2026-08-12
Branch: `main` (repo: `bdrezek91/TV`)
VPS: `~/tradingview-mcp` na `server750497`

## 0. Aktualny stan badań V3–V15

- V3–V14: zamknięte bez promocji; przyczyny opisuje
  `V3_V14_POSTMORTEM.md`.
- V15: wdrożono nową hipotezę BTC-neutral residual reversion, runner,
  zabezpieczenia holdoutu oraz testy jednostkowe i integracyjne.
- Pełna walidacja repozytorium po implementacji: 540 testów przeszło,
  25 pominięto zgodnie z konfiguracją, 8 odznaczono; Ruff i CI były zielone.
- Development V15 wykonano na VPS na już obserwowanym okresie 90 dni.
  Uzyskano 135/183/225 transakcji w trzech harmonogramach.
- Wszystkie harmonogramy były stratne: expectancy od -0,1441 R do -0,1664 R,
  PF od 0,3729 do 0,4699. Klasyfikacja: `DEVELOPMENT_FAIL`.
- V15 nie kwalifikuje się do holdoutu. `holdout_mrv2_120d` nie został użyty.
- Następny etap wymaga nowej prerejestracji V16; nie wolno dostrajać V15 po
  poznaniu jej wyniku.

## 1. Cel

Rozbudowa istniejącego serwera **TradingView MCP** (narzędzia typu `coin_analysis`,
`top_gainers`, `bollinger_scan` itd. — 37 narzędzi, podłączone do Claude jako
connector `TV-connector` pod `https://tvscanai.pl/mcp`) o **automatyczny system
wykrywania okazji tradingowych na Bybit linear futures**, działający w tle,
niezależnie od Claude, z pełną kontrolą ryzyka i **wyłącznie w trybie paper
trading** (żadnych realnych zleceń).

Zamiast żeby Claude za każdym uruchomieniem rutyny liczył wskaźniki na żywo i
sam wymyślał wejście/SL/TP (niedeterministyczne, drogie w tokenach), system w
tle **stale zbiera dane, liczy wskaźniki i ocenia strategie deterministycznym
kodem Pythona**, a rutyna Claude tylko **czyta gotowy wynik**.

## 2. Architektura — przepływ danych

```
Bybit (WebSocket + REST)
        │
        ▼
bybit-collector (Python, kontener Docker)
   • WebSocket: trades, orderbook, likwidacje, tickery, świece 1m
   • REST: open interest, funding, mark/index price, backfill świec 15m/1h/4h
        │
        ▼
PostgreSQL (trading-postgres)
   • candles, trade_aggregates, orderbook_feature_snapshots,
     liquidation_aggregates, derivatives_snapshots
        │
        ▼
analysis-worker (Python, kontener Docker) — cykl co STRATEGY_INTERVAL_SECONDS
   1. Czyta najświeższe dane z Postgresa dla każdego symbolu
   2. Feature Engine — liczy ATR, EMA, CVD, imbalance order booka, OI, likwidacje
   3. Pobiera kontekst TradingView (trend 15m/1h/4h, RSI, MACD, Bollinger)
      — łączy dane Bybit (order flow) z klasyczną analizą techniczną
   4. Market Regime Classifier — TREND_UP/DOWN, RANGE, CHAOTIC, itd.
   5. Strategie: Trend Pullback, Breakout+Retest (Liquidation Reversal — wyłączona)
   6. Scoring Engine (0-100, wagi w config/strategies/v1.yaml)
   7. Hard Gates — twarde odrzucenie niezależnie od score
   8. Risk Engine — wielkość pozycji, limity dzienne, brak martingale
        │
        ▼
PostgreSQL: feature_snapshots, market_regimes, strategy_signals
        │
        ▼
tradingview-mcp (główny serwer MCP, obsługuje connector Claude)
   • Nowe narzędzia: trading_system_health, run_futures_opportunity_scan,
     bybit_market_state, get_trade_setup, paper_portfolio_status,
     strategy_performance, rank_futures_opportunities
   • Czytają WYŁĄCZNIE gotowe dane z bazy — nic nie liczą na żywo
        │
        ▼
Rutyna Claude (co 3h) — woła trading_system_health + run_futures_opportunity_scan
   • Nie przelicza samodzielnie wejścia/SL/TP/score — używa tylko danych z systemu
   • Wysyła PushNotification z wynikiem (setup albo uczciwy NO_TRADE)
```

## 3. Kontenery Docker (docker-compose.yml)

| Kontener | Rola | Restart wymagany po zmianie |
|---|---|---|
| `tradingview-mcp` | Produkcyjny serwer MCP, obsługuje connector Claude. **Nietknięty w Bloku 1-2**, w Bloku 3 dodano 7 nowych narzędzi. Ma customową konfigurację (limity zasobów, `TransportSecuritySettings`, sieć `multiplekser_default`). | Przy zmianie `TRADING_SYMBOLS`/innych zmiennych w `.env`, przy zmianie `server.py` |
| `bybit-collector` | Zbiera dane z Bybit (WebSocket + REST) | Przy zmianie kodu kolektora, `TRADING_SYMBOLS` |
| `analysis-worker` | Liczy features → regime → strategie → score → risk, zapisuje sygnały | Przy zmianie kodu silnika, `TRADING_SYMBOLS`, progów `MAX_DATA_AGE_*` |
| `paper-broker` | Stub infrastrukturalny (logika wykonania zleceń paper istnieje w kodzie, ale pętla nie jest jeszcze spięta na żywo) | — |
| `postgres` | Baza danych, wszystkie tabele systemu | — |
| `redis` | Przygotowany, obecnie nieużywany | — |
| `prometheus` / `grafana` | Metryki i dashboard (dashboard nie zweryfikowany wizualnie — sandbox nie mógł pobrać obrazu Grafany do testu) | — |

**Ważne:** wszystkie 4 kontenery aplikacyjne (`tradingview-mcp`, `bybit-collector`,
`analysis-worker`, `paper-broker`) używają **tego samego obrazu** (`atilaahmet/tradingview-mcp:latest`),
tylko z innym entrypointem. `docker compose build` bez argumentu przebudowuje
wszystkie na raz, ale trzeba **osobno** zrobić `up -d --force-recreate` dla
każdego kontenera, którego dotyczy zmiana — Docker Compose nie restartuje
automatycznie kontenera tylko dlatego, że zmienił się `.env`.

## 4. Kluczowe zmienne w `.env` (sekcja "Trading system extension")

| Zmienna | Aktualna wartość | Znaczenie |
|---|---|---|
| `TRADING_MODE` | `paper` | Wymuszone — system odmawia startu w innym trybie |
| `TRADING_SYMBOLS` | 15 par: BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, LINK, DOT, LTC, SUI, NEAR, TRX, TON (wszystkie USDT-perp) | Lista symboli zbieranych i ocenianych |
| `STRATEGY_INTERVAL_SECONDS` | `900` (15 min) | Jak często `analysis-worker` ocenia wszystkie symbole. Podniesione z 300s, bo 15 symboli × 5 zapytań TradingView/symbol nie mieściło się w 5 min |
| `MAX_DATA_AGE_CANDLES_SECONDS` | `1200` (20 min) | **Naprawione** — było 180s, matematycznie niemożliwe do spełnienia dla świec 15m (patrz sekcja 6) |
| `MAX_DATA_AGE_TRADES_SECONDS` | `120` | **Naprawione** — było 10s, ten sam problem dla wiader 1-minutowych |
| `MAX_DATA_AGE_ORDERBOOK_SECONDS` | `15` | **Naprawione** — było 5s, dokładnie tyle co interwał zapisu (zero marginesu) |
| `SIGNAL_MIN_SCORE` | `75` | Próg score do wygenerowania kandydata |
| `RISK_PER_TRADE_PCT` | `0.25` | Ryzyko na transakcję (paper trading) |
| `ENABLE_LIQUIDATION_REVERSAL` | `false` | Trzecia strategia — zaimplementowana, świadomie wyłączona |

## 5. Historia bugów znalezionych i naprawionych (na żywych danych VPS)

System jest objęty zestawem 533 przechodzących testów hermetycznych, ale kilka błędów
ujawniło się dopiero po realnym wdrożeniu — normalne dla tego typu integracji.
Wszystkie naprawione, przetestowane i włączone do `main`:

1. **`KeyError: source_timestamp`** — świece z WebSocketu nie miały tego pola,
   wymaganego przez regułę "nie nadpisuj nowszych danych starszymi"
2. **`analysis-worker` był pustym stubem** — Blok 1/2 zbudowały cały silnik
   (Feature Engine, strategie, scoring, risk), ale nic nie spinało tego z
   realnymi danymi z bazy w pętli. Dopisana pełna logika cyklu.
3. **Świece 15m/1h/4h nigdy się nie odświeżały** po starcie kolektora (tylko
   jednorazowy backfill) — dodano okresowe doganianie
4. **Trzy niemożliwe do spełnienia progi świeżości danych** — `candles`
   (180s vs naturalne opóźnienie 15-30 min), `trades` (10s vs ~60s), `orderbook`
   (5s = dokładnie tyle co interwał zapisu, zero marginesu). Wszystkie
   podniesione do sensownych wartości.
5. **Zły format symbolu w zapytaniu do TradingView** — `_default_analyzer`
   przekazywał gołe "BTCUSDT" zamiast "BYBIT:BTCUSDT" do
   `run_multi_timeframe_analysis`, co powodowało pozorną "awarię upstream"
   przy każdym cyklu. Naprawione przez normalizację symbolu, tak jak robi to
   publiczne narzędzie `multi_timeframe_analysis`.
6. **`tradingview-mcp` nie widział nowego `TRADING_SYMBOLS`** po rozszerzeniu
   listy symboli, bo zmienił się tylko `.env`, a sam kontener nie został
   zrestartowany (restartowaliśmy tylko `bybit-collector`/`analysis-worker`).
   Rozwiązanie: zawsze `docker compose up -d --force-recreate tradingview-mcp`
   po zmianie `.env`, jeśli dotyczy to zmiennych czytanych przez ten kontener.

## 6. Dlaczego progi świeżości danych były błędne (techniczne wyjaśnienie)

`assess_data_quality()` sprawdza wiek danych względem **timestampu ostatniego
zamkniętego rekordu**, nie względem czasu ostatniego zapisu do bazy. Świeca
15-minutowa z definicji ma `open_time` sprzed 15-30 minut (tyle trwa jej
"życie" zanim się zamknie) — próg 180 sekund nigdy nie mógł zostać
spełniony, niezależnie jak często kolektor odświeżał dane. Analogicznie dla
wiader transakcji 1-minutowych (próg 10s vs naturalne opóźnienie ~60s) i
snapshotów order booka (próg 5s = dokładnie tyle co interwał zapisu, zero
tolerancji na jitter sieci/bazy).

## 7. Rutyna Claude

Nazwa w UI: `Routine` (bez nazwy własnej), harmonogram co 3h.
**Nie została utworzona przeze mnie** (powstała przez `http_api`/UI), więc nie
mam do niej dostępu programistycznego — każda zmiana treści wymaga ręcznej
edycji w UI (claude.ai → Routines).

Aktualna treść: używa `trading_system_health` → `run_futures_opportunity_scan`
(minimum_score=75, maximum_results=2) → `PushNotification`. Nie przelicza
niczego samodzielnie, nie obniża progu, nie szuka alternatyw przy NO_TRADE.

Osobna rutyna **"Forex"** istnieje równolegle i **nie została zmieniona** —
nadal liczy wszystko na żywo starym sposobem (woła stare narzędzia
TradingView bezpośrednio).

## 8. Znane otwarte sprawy

- **Do zweryfikowania jutro:** czy wszystkie 15 symboli osiągnęło
  `is_tradeable=true` po nocy zbierania danych
- **Do zrobienia:** restart `tradingview-mcp` żeby faktycznie widział 15
  symboli zamiast tylko BTCUSDT w `run_futures_opportunity_scan`
  (`docker compose up -d --force-recreate tradingview-mcp`)
- Drobne ostrzeżenie w logach: `UserWarning: Interval is empty or not valid,
  defaulting to 1 day` z biblioteki `tradingview_ta` — niegroźne, nie
  blokuje działania, ale niedociągnięte do końca
- `paper-broker` i pętla realnego wykonywania zleceń paper nie są jeszcze
  spięte na żywo (kod istnieje i jest przetestowany, ale wymaga jeszcze
  wpięcia analogicznie do tego, co zrobiliśmy dla `analysis-worker`)
- Dashboard Grafany nie został zweryfikowany wizualnie (ograniczenie
  sandboxa, w którym budowany był kod — nie VPS)
- Jeszcze nie zaobserwowaliśmy żadnego realnego wygenerowanego sygnału
  (CANDIDATE/REJECTED) — rynek był w niskiej zmienności; to nie błąd, tylko
  brak okazji w tym okresie

## 9. Jak wdrażać zmiany (checklist)

Repo na VPS ma remote `mywork` wskazujący na `bdrezek91/TV` (osobny od `origin`,
który wskazuje na oryginalne, upstream repo `atilaahmettaner/tradingview-mcp`).

```bash
cd ~/tradingview-mcp
git pull mywork main --no-rebase                        # merge, nie rebase — VPS ma własne lokalne commity (docker-compose.yml)
docker compose build <serwis>                              # tylko zmienione serwisy
docker compose up -d --force-recreate <serwis>              # wymusza realny restart
```

Po zmianie w `.env` (np. progów, listy symboli) — zawsze sprawdź, **które
kontenery faktycznie czytają tę zmienną** i zrestartuj wszystkie z nich, nie
tylko te oczywiste (patrz bug #6 wyżej — `tradingview-mcp` też czyta
`x-trading-env`).

## 10. Bezpieczeństwo / zasady

- `TRADING_MODE=paper` wymuszone na starcie — brak jakiegokolwiek kodu
  składającego realne zlecenia w całym systemie (potwierdzone grepem)
- AI (Claude) nigdy nie liczy samodzielnie wejścia/SL/TP/score — wszystko
  pochodzi z deterministycznego kodu Pythona
- Każdy sygnał (także odrzucony) jest zapisywany do bazy z pełnym powodem
- Dokument `docs/trading-system/LIVE_TRADING_GATE.md` opisuje wymagania
  przed jakimkolwiek rozważeniem live tradingu (na razie tylko checklist,
  zero implementacji)
