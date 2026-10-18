# DMZ v2.2 training minigames (rhythm, ki control, memory, precision, gravity)

Five training minigames give TP (config id in brackets): Rhythm (`rhythm`), Ki Control (`control`), Shadow Boxing (`memory`, type the combo shown), Precision (`precision`), Gravity (`gravity`). Play them from the Minigames tab of the menu (V) once learned.

Masters: Rhythm Mr. Popo (inside the Time Chamber), Ki Control Krillin, Shadow Boxing Gohan, Precision Trunks, Gravity Vegeta. Play it from the master 3 times reaching level 5 to learn it (`learnRequiredRuns`, `learnRequiredLevel`); until then the menu says "Visit its master first." Starting a known game from the master's menu pays +25% (`mentorBonus`). Old Kai's Ultimate Challenge chains every minigame.

Rewards scale with how long you play, your level reached and your performance (`tpPerMinuteCoefficient` 62, `minPerformanceRewardMultiplier` 0.75 to `maxPerformanceRewardMultiplier` 1.25). Challenges target 10 levels above you.

Rhythm: lanes A, S, K, L. Song mode needs the music disc in your inventory; 75% accuracy "learns" the song so the disc isn't needed. Free mode needs no song. Charts are cached in `<game dir>/dragonminez/rhythm_charts/`. Off-beat notes: change `rhythmAudioOffsetMs` (60) in general-user.json.

All tuning in `config/dragonminez/training.json`. Admin: `/dmzminigame play <minigame> [targets] [masterBonus]`, `/dmzminigame learn <minigame> [targets]`.

Keywords: minigame, minigames, training, rhythm game, music disc, ki control, memory game, precision, gravity minigame, learn minigame, not learned, training.json, rhythm offset, out of sync
