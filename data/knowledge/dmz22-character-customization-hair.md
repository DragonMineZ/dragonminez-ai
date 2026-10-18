# DMZ v2.2 character customization, hair editor and pixel skin editor

Character creation opens on first join when `forceCharacterCreation` is true (general-server gameplay). Pick race, gender (Human, Saiyan, Majin, Glind), class (last tab, Class & Aura), body type, colors, eyes, nose, mouth, tattoos and hair. The Re-Customize wish reopens it to change looks and class (not race), keeping stats and skills. Changing race needs a full character reset (Dende's Reset Stats service, wipes progress). Mr. Popo's Haircut reopens the hair editor any time. Humans can become Androids through Dr. Gero.

Hair editor: custom hairstyles are built from strands. Server limits in `general-server.json` `hair`: `maxSegmentsPerStrand` (12), `maxStrandLength` per hair slot (base, ssj, ssj2, ssj3, ssj4; 20), `maxStrandWidth` (8), `maxRootOffset` (2), `maxTotalSegmentsPerStyle` (600). Styles beyond the limits are trimmed. Old hair data from earlier versions is migrated automatically. Hair physics quality is a client option.

Pixel skin editor: paint skin layers and tattoos (`pixelTattooPaintingEnabled` in general-server gameplay). Larger tattoo sizes are reserved for beta testers.

Fixes: `/dmzhair resync [targets]` resends hair data if it looks wrong for others; `/dmzhair reset [targets]` resets it. Saiyan tail: `/dmztail cut|grow`.

Keywords: character creation, customize, customization, hair, hair editor, hairstyle, custom hair, hair broken, hair not showing, skin editor, tattoo, pixel editor, recustomize, dmzhair, tail
