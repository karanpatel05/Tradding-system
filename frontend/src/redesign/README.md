# Operator console preview

Run from `frontend`:

```sh
npm run dev -- --host 127.0.0.1 --port 5174
```

Open http://127.0.0.1:5174/redesign.html.

Build independently of the existing application:

```sh
npx vite build --config redesign.config.js
```

The output is `frontend/dist-redesign`. The existing production entry and backend are unchanged.

## Scope

Interactive design sample covering Overview, Strategies, Options builder, Journal, and Connections. All data is fictional. Controls use browser-local React state, reset on reload, and never call broker or application APIs. The builder implements the expiration payoff of a bull call spread with illustrative premiums, not a pricing engine. Portfolio history and research statistics are fixtures. The preview uses IBM Plex Sans from Google Fonts with a system-font fallback.

The design prioritizes positions, allocation, and system health, with a dark navigation rail and restrained blue accents on a light workspace. It is a reviewable direction for a later production integration, not a replacement for the full existing functionality.

## Verified

- Independent Vite production build passed (non-blocking bundle-size warning).
- All five views rendered in the browser.
- Quantity changes doubled spread debit and maximum profit/loss correctly; saved spread appeared in Tracked trades.
- Strategy pause, demo disconnect, and emergency halt changed visible state.
- Journal month navigation changed the calendar.
- 390px responsive view had no horizontal page overflow.
- No browser console errors observed.

Screenshots of the implemented overview and builder are in `frontend/redesign-preview`.
