# V15 — postmortem fazy development

> Status końcowy: `DEVELOPMENT_FAIL`. V15 została zamknięta bez promocji.
> Nie wolno uruchamiać jej na nowym holdoucie ani wykorzystywać w handlu live.

Data oceny: 2026-08-12  
Commit badanego kodu: `d3b9355`  
Okres development: 2026-04-12 – 2026-07-10  
Źródło: obserwowany wcześniej cache `holdout_90d/cache` użyty wyłącznie jako
`POST_HOLDOUT_DEVELOPMENT_NOT_VALIDATION`.

## Wynik bramki

| Harmonogram | Transakcje | Win rate | Expectancy | Profit factor | PnL netto |
|---|---:|---:|---:|---:|---:|
| CURRENT_DAYTIME_2H_07_21 | 135 | 27,41% | -0,1441 R | 0,4699 | -1945,39 |
| FULL_24H_2H | 183 | 22,95% | -0,1562 R | 0,4195 | -2858,43 |
| FULL_24H_1H | 225 | 18,67% | -0,1664 R | 0,3729 | -3744,90 |

V15 nie spełniła żadnej bramki jakościowej dotyczącej przewagi:

- 0 z wymaganych 2 harmonogramów miało dodatnie expectancy;
- 0 z wymaganych 2 harmonogramów osiągnęło PF co najmniej 1,15;
- każdy harmonogram przekroczył katastroficzny próg -0,10 R;
- 0 harmonogramów miało dodatnią dolną granicę 95% CI;
- próba 225 transakcji w FULL_24H_1H była wystarczająca;
- wspólne zdarzenia cadence były deterministyczne
  (`common_r_mismatches = 0`);
- `holdout_mrv2_120d` nie został użyty.

## Audyt przyczyny

Implementacja jest zgodna z prerejestracją:

- detektor kwalifikuje każdy sygnał z `|z| >= 2,0`;
- symulator sprawdza `|z| >= 3,5` jako stop od pierwszej wspólnej minuty
  po wejściu;
- opłaty, slippage i konserwatywny funding są naliczane dla obu nóg;
- selekcja pary i globalny lifecycle odbywają się przed oceną wyniku;
- nie stwierdzono rozbieżności R dla wspólnych zdarzeń cadence.

Oznacza to wadę zamrożonej hipotezy, a nie przypadkową awarię runnera.
Specyfikacja dopuszcza wejście już poza poziomem własnego stopa. W danych
występują wejścia z `|z| > 3,5`, po których stop jest wykonywany niemal
natychmiast. Nie wolno naprawiać tego przez zmianę V15 po poznaniu wyniku.

Dominującym źródłem strat był `PAIR_STOP`:

- FULL_24H_1H: 165 z 225 transakcji (73,3%), expectancy -0,3356 R;
- FULL_24H_2H: 122 z 183 transakcji (66,7%), expectancy -0,3673 R;
- CURRENT_DAYTIME_2H_07_21: 88 z 135 transakcji (65,2%),
  expectancy -0,3826 R.

Właściwa rewersja residualu była zyskowna, ale zbyt rzadka, aby pokryć częste
stopy i koszty. Wszystkie cztery pary oraz oba kierunki miały ujemne
expectancy w pełnym harmonogramie 1H.

## Koszty

| Harmonogram | PnL netto | Fee | Slippage | Funding | PnL przed raportowanymi kosztami |
|---|---:|---:|---:|---:|---:|
| CURRENT_DAYTIME_2H_07_21 | -1945,39 | 1484,01 | 539,64 | 110 | ok. +188,26 |
| FULL_24H_2H | -2858,43 | 2011,82 | 731,57 | 131 | ok. +16,95 |
| FULL_24H_1H | -3744,90 | 2473,85 | 899,58 | 139 | ok. -232,47 |

Nawet przed kosztami hipoteza miała co najwyżej śladową przewagę. Konstrukcja
dwunożna i wysoka rotacja usuwały ją po realistycznych kosztach.

## Decyzja

1. V15 pozostaje historycznym, odtwarzalnym eksperymentem.
2. Wynik klasyfikujemy jako `DEVELOPMENT_FAIL`.
3. Nie tworzymy holdoutu V15 i nie dotykamy `holdout_mrv2_120d`.
4. Nie zmieniamy parametrów V15 po wyniku.
5. Dalsze badania muszą być nową, prerejestrowaną hipotezą (V16), z jawnie
   rozłącznymi warunkami wejścia i stopa oraz kontrolą kosztów przed
   uruchomieniem.
