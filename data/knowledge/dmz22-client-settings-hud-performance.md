# DMZ v2.2 client settings, HUD editor and performance options (general-user.json)

`config/dragonminez/general-user.json` is per player (never synced from the server). Also editable in the menu's Config tab. Options:
- Visuals: `firstPersonAnimated`, `impactFramesEnabled`, `transformationOutlines`, `aura3DPersonal` and `aura3DEntities` (3D auras, heavier), `aura3DStyle`, `hairPhysicsQuality`, `taiyokenInvertPalette` (inverts the Solar Flare flash colors).
- HUD: `alwaysVisibleHudValues`, `hideHudNumbers`, `showAccumulativeDamage`, `partyMarkers`, `hexagonStatsDisplay`, `menuScaleMultiplier`, `utilityMenuScaleMultiplier`.
- Camera: `overShoulderMode` and offsets, `cameraMovementDuringFlight`, `flightTurnSensitivity`.
- Other: `kiSoundVolume`, `reserveFirstHotbarSlot`, `tutorialsEnabled`, `advancedDescription`, `rhythmAudioOffsetMs`.

Most options are in the Character Menu (V) > Config (Interface, Gameplay, Visuals, Audio) and apply immediately; hand edits need a game restart. HUD editor: V > Config > Interface > HUD Layout (4 styles; move, resize, mirror or hide elements), saved in `hud_layout.json`.

Low FPS: turn off 3D auras, lower hair physics quality, turn off impact frames and transformation outlines. If auras or outlines look broken with a shader pack, test without the shader pack first.

Keywords: client settings, general-user.json, hud, hud editor, move hud, hud layout, fps, lag, performance, 3d aura, hair physics, impact frames, outlines, camera, over the shoulder, tutorials
