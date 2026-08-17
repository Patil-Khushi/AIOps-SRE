## Summary
- Add a dimmed/blurred backdrop behind the RCA chat panel and make the page behind it inert while open
- Restructure the panel into a fixed header, scrollable message thread, and fixed footer so the input bar no longer overlaps messages on scroll
- Fix the floating launcher bubble overlapping the panel's own close button by fading it out while the panel is open instead of swapping to a second X
- Restyle message bubbles/composer/header close button to match the target chat design
- Show the launcher bubble immediately on the Incidents list and the incident workspace (auto-scoped to the top/current incident) instead of only after clicking "Debug"
- Remove the raw alert-title line from the chat header
- Rename the "Incident Command Center" page heading to "Root Cause Analysis"

## Test plan
- [ ] `npm run dev` in `demo/dashboard`, open `/console/incidents` — launcher bubble appears immediately, no Debug click needed
- [ ] Open an incident workspace directly via URL — launcher appears
- [ ] Open the chat panel — backdrop dims/blurs the page and background is unclickable; scrolling the thread doesn't overlap the input bar; close button and send button don't collide
- [ ] Click "Debug" on a non-top row — dock scopes to that row and isn't overridden by the list's auto-focus
