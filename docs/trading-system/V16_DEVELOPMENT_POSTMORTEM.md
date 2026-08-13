# V16 — postmortem fazy development

> Status końcowy: `DEVELOPMENT_FAIL`. V16 została zamknięta bez promocji.
> Nie wolno uruchamiać jej na nowym holdoucie ani wykorzystywać w handlu live.

Data oceny: 2026-08-13
Commit badanego kodu: `0f14f23`
Okres development: 90 dni ewaluacji + 14 dni warmup, cache pobrany 2026-08-13
Źródło: `extended_history` cache (ten sam mechanizm co V15), orderbook i
likwidacje `DEGRADED` (patrz `V16_PREREGISTRATION.md`, sekcja "Znane
ograniczenie fazy development") — ocena na maks. 7/11 wymiarów order-flow.

## Wynik bramki

| Harmonogram | Transakcje | Win rate | Expectancy | Profit factor | PnL netto |
|---|---:|---:|---:|---:|---:|
| CURRENT_DAYTIME_2H_07_21 | 113 | 36,28% | -0,0884 R | 0,6783 | -999,15 |
| FULL_24H_2H | 160 | 35,63% | -0,1237 R | 0,5706 | -1979,94 |
| FULL_24H_1H | 274 | 33,94% | -0,1493 R | 0,5150 | -4092,10 |

V16 nie spełniła 5 z 7 kryteriów bramki:

- 0 z wymaganych 2 harmonogramów miało dodatnie expectancy;
- 0 z wymaganych 2 harmonogramów osiągnęło PF co najmniej 1,15;
- każdy harmonogram przekroczył katastroficzny próg -0,10 R
  (najbliżej granicy `CURRENT_DAYTIME_2H_07_21` przy -0,0884 R, ale
  `FULL_24H_2H` i `FULL_24H_1H` już poniżej);
- 0 harmonogramów miało dodatnią dolną granicę 95% CI;
- **żaden symbol nie miał dodatniego PnL netto** — koncentracja na
  pojedynczym symbolu jest formalnie niepoliczalna (`null`), bo nie ma z
  czego liczyć udziału w dodatnim PnL;
- próba 274 transakcji w FULL_24H_1H była wystarczająca (≥100);
- wspólne zdarzenia cadence były deterministyczne
  (`common_r_mismatches = 0`).

## Audyt przyczyny

W przeciwieństwie do V15, implementacja V16 miała rozłączne źródła danych
dla wejścia (order-flow, `confirm_setup`) i stopa (ATR na cenie) — ten
konkretny błąd konstrukcyjny z V15 się nie powtórzył. Mimo to strategia
nie wykazała przewagi. Rozkład wyjść w `FULL_24H_1H` (274 transakcji)
pokazuje, gdzie:

| Powód wyjścia | Transakcje | Udział | Expectancy | PnL netto | Udział w stracie |
|---|---:|---:|---:|---:|---:|
| `ATR_STOP` | 24 | 8,8% | -1,1345 R | -2722,83 | 66,5% |
| `ORDERFLOW_REVERSAL` | 250 | 91,2% | -0,0548 R | -1369,27 | 33,5% |

Kluczowa obserwacja: **wyjścia przez `ORDERFLOW_REVERSAL` (91% wszystkich
transakcji) mają expectancy statystycznie nieodróżnialne od zera**
(95% CI: -0,118 R do +0,009 R, górna granica prawie dotyka zera). Sam
rdzeń hipotezy — "trzymaj pozycję dopóki zagregowany order-flow się nie
odwróci" — nie jest wyraźnie stratny. Cała strata netto pochodzi
nieproporcjonalnie z rzadkich (8,8% transakcji) trafień w `ATR_STOP`,
każde ze średnią stratą -1,13 R (gorszą niż nominalne -1 R przez koszty
fee/slippage doliczone na stracie). Innymi słowy: mała liczba "ogonowych"
strat na stopie przeważa nad w większości neutralnym rdzeniem strategii —
zyski z pozycji trzymanych do odwrócenia order-flow nie są wystarczająco
duże ani częste, żeby skompensować te rzadkie, ale kosztowne wybicia
stopa.

### Rozkład per symbol (FULL_24H_1H)

| Symbol | Transakcje | Expectancy | PnL netto |
|---|---:|---:|---:|
| XRPUSDT | 36 | -0,2689 R | -967,99 |
| BNBUSDT | 24 | -0,2444 R | -586,55 |
| ETHUSDT | 77 | -0,1764 R | -1358,14 |
| BTCUSDT | 71 | -0,1214 R | -861,71 |
| SOLUSDT | 66 | -0,0481 R | -317,70 |

Wszystkie pięć symboli miało ujemne expectancy — brak koncentracji na
jednym instrumencie (co samo w sobie nie ratuje wyniku, bo bramka i tak
wymaga dodatniego PnL choćby na jednym, a nie ma żadnego). SOLUSDT był
najbliżej zera, ale przy tylko 66 transakcjach (`PRELIMINARY`) i CI
obejmującym zero (-0,196 R do +0,122 R) — brak dowodu na przewagę nawet
tam.

## Koszty

Fee + slippage w `FULL_24H_1H` pochłonęły łącznie 3764,46 (2760,61 fee +
1003,86 slippage) przy PnL netto -4092,10 — koszty same w sobie nie są
głównym wyjaśnieniem straty (w przeciwieństwie do V15, gdzie dwunożna
konstrukcja podwajała koszty transakcyjne); tutaj dominuje jakość samej
tezy (rzadkie, ale duże straty na stopie), nie fryktion.

## Decyzja

1. V16 pozostaje historycznym, odtwarzalnym eksperymentem.
2. Wynik klasyfikujemy jako `DEVELOPMENT_FAIL`.
3. Nie tworzymy holdoutu V16 i nie dotykamy `holdout_mrv2_120d`.
4. Nie zmieniamy parametrów V16 po wyniku (np. nie zawężamy ATR-multiple,
   nie zmieniamy progu `CONFIRMED`, nie filtrujemy post-hoc symboli/godzin).
5. Zaobserwowany wzorzec — rdzeń tezy blisko zera, rzadkie duże straty na
   stopie dominujące wynik — sugeruje dla V17 (jeśli powstanie): albo
   szerszy stop (mniej "ogonowych" wybić kosztem większego ryzyka na
   transakcję), albo mechanizm redukujący rozmiar pozycji przy najsłabszym
   potwierdzeniu order-flow, ale to nowa, osobno prerejestrowana hipoteza —
   nie retusz V16.
