# UI art

`Card Outline .png` is drawn only around cards placed on the board. It is not drawn on deck/hand cards or the enlarged reveal card. Its near-black background and smoky center are keyed to transparency. Board slots keep their normal state outlines. If the art is unavailable, placed cards keep their built-in outline.

Adjust its size in `config/settings.json` under `card_frame.placed`. `width` and `height` are independent multipliers: `1.0` matches the card size, `0.9` makes that dimension 90%, and `1.1` makes it 110%.
