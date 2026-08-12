# V16 — prerejestracja strategii order-flow-triggered regime-gated (single-instrument)

> Status: **PROJEKT — niezamrożony.** Ten dokument nie jest jeszcze
> zamrożoną specyfikacją. Zamrożenie następuje dopiero po jawnej zgodzie
> użytkownika; od tego momentu żaden próg w tym pliku nie może się zmienić
> na podstawie wyniku development.

## Hipoteza

Zagregowany, wielowymiarowy odczyt order-flow (11 niezależnych wymiarów z
`orderflow_confirmation_v2`: CVD, taker buy/sell, OI, funding, long/short
ratio, likwidacje, orderbook imbalance, spread/depth, microprice, duże
transakcje, zgodność z reżimem BTC) sam w sobie niesie krótkoterminową
przewagę kierunkową — pod warunkiem że rynek jest w reżimie, w którym
order-flow ma sens jako lead indicator (trend/breakout), a nie w
RANGE/NO_EDGE/panice/niestabilnych danych.

To odróżnia V16 od V3–V14 (setupy cenowe z order-flow jako filtrem wtórnym)
i od V15 (dwunożna rewersja residualu z-score): tutaj order-flow **jest**
triggerem wejścia, nie potwierdzeniem czegoś innego, i strategia jest
jednonożna (pojedynczy instrument, bez drugiej nogi BTC).

## Dlaczego rozłączność wejście/stop (lekcja z V15)

V15 padła częściowo dlatego, że wejście (`|z| >= 2,0`) i stop (`|z| >= 3,5`)
były liczone **tą samą zmienną** — możliwe było wejście już przy `|z| > 3,5`,
czyli faktycznie za własnym stopem. V16 definiuje wejście i stop na
**dwóch niezależnych źródłach danych**, które nie mogą nachodzić się z
konstrukcji:

- Wejście: zagregowany score order-flow (funkcje `_dim_*` z
  `orderflow_confirmation_v2`, importowane bez duplikacji logiki).
- Stop: czysto cenowy, ATR-based (`price_action` z `feature_engine`),
  liczony niezależnie od jakiegokolwiek odczytu order-flow.

## Zamrożona specyfikacja (projekt)

- **Uniwersum**: `BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, BNBUSDT` — ten sam
  zamrożony zestaw pięciu symboli co `REQUIRED_SYMBOLS` (spójność z resztą
  pipeline'u), każdy oceniany **niezależnie, jednonożnie** (nie para).
- **Dane sygnałowe**: zsynchronizowane snapshoty 1H zamknięte przed decyzją
  — regime (Warstwa 1), volume/CVD, orderbook, futures (OI/funding/L-S),
  likwidacje — te same kształty danych co `orderflow_confirmation_v2`
  i `regime_classifier_v2` już konsumują.
- **Regime gate**: sygnał dozwolony tylko gdy `primary_regime` ∈
  `{TREND_UP, TREND_DOWN, BREAKOUT_UP, BREAKOUT_DOWN}`. Wyklucza RANGE,
  SQUEEZE, HIGH_VOLATILITY, PANIC_LIQUIDATION_*, LOW_LIQUIDITY,
  UNSTABLE_DATA, NO_EDGE. Brak wymogu zgodności kierunku reżimu z
  kierunkiem order-flow (unikamy dodatkowego przeszukiwania kombinacji po
  fakcie).
- **Data quality gate**: `data_quality_score >= 50` (ten sam próg co
  `regime_classifier_v2.MIN_DATA_QUALITY_SCORE`); poniżej — brak sygnału,
  nie neutralny odczyt.
- **Wejście**: policz `net_score = confirms - contradicts` z 11 wymiarów
  `orderflow_confirmation_v2._dim_*`, osobno dla kandydata LONG i SHORT.
  Wymagane `available_dimensions >= 6/11`. Sygnał LONG gdy
  `net_score(LONG) >= 5`; sygnał SHORT gdy `net_score(SHORT) >= 5`. Jeśli
  oba warunki spełnione jednocześnie (nie powinno się zdarzyć przy
  symetrycznej konstrukcji wymiarów) — sygnał odrzucony jako niejednoznaczny.
- **Przy kilku sygnałach w tym samym skanie**: brak globalnego limitu —
  pozycje są niezależne per symbol (bez wspólnej nogi BTC jak w V15), każdy
  kwalifikujący się symbol dostaje własną pozycję, maks. 1 otwarta pozycja
  na symbol jednocześnie.
- **Wejście (cena)**: zamknięcie świecy decyzyjnej, bez lookahead.
- **Stop**: `1,5 × ATR(14)` na interwale sygnałowym, liczone w momencie
  sygnału, niezależnie od odczytu order-flow.
- **Wyjście (reversal)**: `net_score` dla pierwotnego kierunku spada do
  `<= 0` przy kolejnej ocenie na tym samym harmonogramie.
- **Wyjście (czas)**: maks. 12 godzin (krócej niż V15 — to ma być szybsza,
  reaktywna teza, nie wolna rewersja).
- **Ryzyko na R**: 1% kapitału, sizing od `worst_case` (cena wejścia, bo
  wejście jest po close, nie strefą).
- **Koszty**: fee 0,055%, slippage 2 bps (spójne z V15); funding
  konserwatywny 1 bp brutto za każde pełne 8 godzin (hold może objąć jeden
  interwał funding).

## Bramka development

V16 może zostać zamrożone do przyszłej walidacji tylko wtedy, gdy bez
zmiany specyfikacji spełni łącznie:

1. minimum 100 zakończonych pozycji w `FULL_24H_1H`;
2. dodatnie expectancy w co najmniej dwóch harmonogramach
   (`CURRENT_DAYTIME_2H_07_21`, `FULL_24H_2H`, `FULL_24H_1H`);
3. PF co najmniej 1,15 w co najmniej dwóch harmonogramach;
4. żadnego harmonogramu z expectancy poniżej -0,10 R;
5. dodatnią dolną granicę 95% CI w co najmniej jednym harmonogramie;
6. brak pojedynczego symbolu odpowiadającego za ponad 50% dodatniego PnL
   (próg surowszy niż V15's 60%, bo mamy 5 niezależnych symboli zamiast
   4 par uwiązanych do BTC);
7. brak rozbieżnych wyników R dla wspólnych zdarzeń cadence 1H/2H.

Niespełnienie bramki zamyka V16. Nie wolno ratować wyniku zmianą progów
(net_score, ATR-multiple, regime gate, hold time) na tym samym okresie.

## Ochrona holdoutu

`holdout_mrv2_120d` pozostaje zarezerwowany dla MR V2; V16 go nie dotyka.
Jeśli V16 przejdzie development, otrzyma **własny, nowy, nietknięty okres**
dopiero po zamrożeniu kodu, konfiguracji, commit SHA i reguły PASS/FAIL —
tak jak przewidziano dla V15.
