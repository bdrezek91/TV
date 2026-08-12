# V15 — prerejestracja strategii BTC-neutral residual reversion

> Status: `POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION`. Specyfikacja jest zamrożona
> przed uruchomieniem V15 na wynikach. Nie jest przeznaczona do handlu live.

## Hipoteza

Ekstremalne, krótkoterminowe odchylenie ceny altcoina od jego bieżącej
wrażliwości na BTC może powracać do średniej. Dwie jednoczesne nogi mają
ograniczać wpływ kierunku całego rynku.

## Zamrożona specyfikacja

- Uniwersum: ETHUSDT, SOLUSDT, XRPUSDT i BNBUSDT względem BTCUSDT.
- Dane sygnałowe: wyłącznie zsynchronizowane świece 1H zamknięte przed decyzją.
- Lookback: 72 stopy zwrotu, czyli 73 wspólne zamknięcia.
- Hedge ratio: beta OLS stóp zwrotu alt/BTC liczona na tym samym oknie.
- Spread: `log(alt) - beta * log(BTC)`.
- Wejście: `|z| >= 2,0`; dodatnie z oznacza SHORT alt/LONG BTC, ujemne z
  oznacza LONG alt/SHORT BTC.
- Przy kilku sygnałach w tym samym skanie wybierane jest największe `|z|`;
  remis rozstrzyga stała kolejność symboli.
- Wyjście: `|z| <= 0,5`, `|z| >= 3,5`, strata pary 1% kapitału albo 24 godziny.
- Ekspozycja brutto: 100% kapitału podzielone beta-neutralnie między obie nogi.
- Ryzyko używane do R: 1% kapitału.
- Koszty każdej nogi i strony: fee 0,055% i slippage 2 bps.
- Finansowanie: konserwatywny koszt 1 bp brutto za każde pełne 8 godzin.
- Jedna aktywna para globalnie, ponieważ każda pozycja wykorzystuje BTC.
- Brak filtrów symbolu, kierunku, godziny, W3 lub reżimu.

## Bramka development

V15 może zostać zamrożone do przyszłej walidacji tylko wtedy, gdy bez zmiany
specyfikacji spełni łącznie:

1. minimum 100 zakończonych pozycji w `FULL_24H_1H`;
2. dodatnie expectancy w co najmniej dwóch harmonogramach;
3. PF co najmniej 1,15 w co najmniej dwóch harmonogramach;
4. żadnego harmonogramu z expectancy poniżej -0,10 R;
5. dodatną dolną granicę 95% CI w co najmniej jednym harmonogramie;
6. brak pojedynczej pary odpowiadającej za ponad 60% dodatniego PnL;
7. brak rozbieżnych wyników R dla wspólnych zdarzeń cadence 1H/2H.

Niespełnienie bramki zamyka V15. Nie wolno ratować wyniku zmianą parametrów na
tym samym okresie.

## Ochrona holdoutu

`holdout_mrv2_120d` jest zarezerwowany dla MR V2 i runner V15 odrzuca tę ścieżkę.
Jeśli V15 przejdzie development, otrzyma własny nowy, nietknięty okres dopiero
po zamrożeniu kodu, konfiguracji, commit SHA i reguły PASS/FAIL.

