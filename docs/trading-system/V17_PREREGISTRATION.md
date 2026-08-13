# V17 — prerejestracja strategii order-flow-triggered regime-gated, szerszy stop

> Status: ZAMROŻONA. Zamrożona w commicie `9c96552`. Development
> jeszcze nie uruchomiony.

## Hipoteza

V16 wykazała, że rdzeń tezy order-flow (wyjście przez odwrócenie
`net_independent_score`, 91% transakcji w `FULL_24H_1H`) miał expectancy
statystycznie nieodróżnialne od zera (95% CI: -0,118 R do +0,009 R). Cały
ujemny wynik netto pochodził z rzadkich (8,8% transakcji) trafień w
ciasny stop `1,5×ATR(14)`, każde ze średnią stratą -1,13 R. Hipoteza V17:
**ten sam trigger order-flow, przy szerszym stopie, rzadziej wybija
pozycje na szumie zmienności wewnątrz 12-godzinnego okna, co powinno
poprawić expectancy bez zmiany rdzenia tezy.**

To jest **pojedyncza, izolowana zmiana** względem V16 (ten sam entry
trigger, ten sam regime gate, ten sam exit-by-reversal, ten sam max hold),
zgodnie z zasadą "jedna zmienna na eksperyment" stosowaną już przy
V3 (izolacja setupów). Nie jest to nowa rodzina hipotez — to bezpośredni,
kontrolowany follow-up V16.

## Dlaczego to nie jest "ratowanie V16 po wyniku"

V16 jest zamknięta i niezmieniona — `comparison_orderflow_trigger_v16.py`
oraz jej zamrożone stałe (`ATR_MULTIPLE = 1.5`) pozostają nietknięte,
odtwarzalne, z własnym postmortemem. V17 to **nowy, osobno zamrożony
moduł** (`comparison_orderflow_trigger_v17.py`) z inną stałą
(`ATR_MULTIPLE = 3.0`), testowany na tym samym, już zaobserwowanym okresie
development — dlatego jest jawnie nazwana i traktowana jako kolejny
eksperyment w serii, nie jako "V16 z poprawką". Development V17 może się
nie udać tak samo jak V16; to nie jest gwarantowany sukces, tylko
kontrolowany test jednej, konkretnej hipotezy o przyczynie porażki V16.

## Zamrożona specyfikacja

Identyczna z V16 (`V16_PREREGISTRATION.md`) we wszystkim poza stopem:

- **Uniwersum, dane, regime gate, data quality gate, entry (order-flow
  przez `confirm_setup`), konkurencyjność pozycji, cena wejścia, wyjście
  (reversal), wyjście (czas 12h), ryzyko na R (1%), koszty** — bez zmian,
  patrz `V16_PREREGISTRATION.md`.
- **Stop**: `3,0 × ATR(14)` na interwale sygnałowym (podwojone względem
  V16's `1,5×ATR`), liczone w momencie sygnału, niezależnie od odczytu
  order-flow — ta sama zasada rozłączności co w V16.

Sizing pozostaje 1% ryzyka na transakcję; przy szerszym stopie oznacza to
mniejszy nominalny rozmiar pozycji na transakcję (ten sam R-multiple przy
trafieniu stopa), ale mniej transakcji powinno w ogóle dotrzeć do stopa.

## Bramka development

Identyczna z V16 (ta sama bramka, ten sam sposób oceny):

1. minimum 100 zakończonych pozycji w `FULL_24H_1H`;
2. dodatnie expectancy w co najmniej dwóch harmonogramach;
3. PF co najmniej 1,15 w co najmniej dwóch harmonogramach;
4. żadnego harmonogramu z expectancy poniżej -0,10 R;
5. dodatnią dolną granicę 95% CI w co najmniej jednym harmonogramie;
6. brak pojedynczego symbolu odpowiadającego za ponad 50% dodatniego PnL;
7. brak rozbieżnych wyników R dla wspólnych zdarzeń cadence 1H/2H.

Niespełnienie bramki zamyka V17. Nie wolno ratować wyniku zmianą progów
(ATR-multiple, regime gate, hold time) na tym samym okresie — jeśli
`3,0×ATR` też zawiedzie, to jest wynik, nie punkt wyjścia do kolejnej
korekty w tym samym pliku.

## Znane ograniczenie fazy development

Identyczne jak w V16: orderbook i likwidacje są niedostępne w cache'u
development (`DEGRADED_NO_HISTORICAL_ORDERBOOK` /
`DEGRADED_NO_HISTORICAL_LIQUIDATIONS`), więc ocena entry triggera opiera
się na maksymalnie 7/11 wymiarów `confirm_setup`, tak samo jak w V16.

## Ochrona holdoutu

`holdout_mrv2_120d` pozostaje zarezerwowany dla MR V2; V17 go nie dotyka.
Jeśli V17 przejdzie development, otrzyma własny, nowy, nietknięty okres
dopiero po zamrożeniu kodu, konfiguracji, commit SHA i reguły PASS/FAIL —
tak jak przewidziano dla V15/V16.
