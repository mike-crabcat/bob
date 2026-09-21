# negotiate / event_plan goals — humans on the other side

The shape: an arrangement that exists only when specific people have
confirmed specific things. The counterpart controls the clock; your job is
to make every wait explicit and every silence bounded.

## Bad

> objective: "Sort out the venue with Thomas for the 27th"

Who must confirm what, by when, and what happens on silence? This goal
waits on a vibe.

## Good

> objective: "The 27th is booked: venue confirmed in writing (booking
> reference recorded) AND Thomas has confirmed attendance in his own words
> (reply quoted in evidence). Both confirmations exist as evidence lines."
>
> branches: task "obtain venue booking reference" (completer = the email
> thread), task "obtain Thomas's confirmation for the 27th"
> (completer = the DM conversation with Thomas, due Thursday 18:00).
> ladder (in state block): no reply by due → re-DM next morning → email
> that evening → call the day after.

## The mechanics that make it work

- **Confirmations are tasks whose expected_completer is the channel
  conversation talking to that person.** Their reply lands in that
  conversation; that turn settles the task with the quoted reply; your
  room wakes with the evidence. You never "remember to check".
- **Every wait has a due.** The task_due backstop wakes the room when a
  confirmation is late — the ladder rungs (re-DM → email → call) live in
  the state block and execute as new tasks with new dues.
- **Never post status checks to groups.** Chase individuals in their DMs;
  groups hear outcomes, not process.
- **Hard dates only from written words.** "Probably the 27th" is an
  open_question, not a known fact (the 2026-09-13 booked-set denial and
  the 2026-09-02 announce-without-evidence incidents are this exact
  failure).

## Cues you're writing one

"get X to agree", "arrange", "book", "confirm with". If the output is a
human's commitment, it's negotiate; a multi-party event is event_plan
with one confirmation-task per party.
