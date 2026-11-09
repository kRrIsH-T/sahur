# Form assistant demos

Start the local fixture server in a terminal:

```powershell
uv run python docs/demo/serve.py
```

Open `http://127.0.0.1:8765/form_smoke.html` in a separate browser tab. It has three labelled fields and a
Submit button. The English bank form, Hindi form and deliberately suspicious page exercise manual-only
controls, OTP/password refusal, and unsupported controls. Use invented values throughout.

In a second terminal, select English for the installed English Windows voice and start voice mode:

```powershell
$env:USER_LANGUAGE = 'en-IN'
uv run python -m bolos
```

Keep the demo browser window in front when the form command is handled; the UI Automation backend captures
the foreground document and refuses to change another window. Voice mode avoids switching to a terminal to
issue the initial command. Text mode is useful for dry runs and injected-backend tests; a terminal remains
the foreground window when a typed command is captured, so it cannot directly target the browser form.

Say “what does this form need” to get local field guidance, or “help me fill this form” to start a session.
The first time, the session asks for consent to online speech recognition. Then each field in turn is ringed
and focused, asked for, and filled as soon as the answer is heard; say “galat” to redo the previous one.
Suggested fake values for the smoke page are `Asha Example`, `9000000000`, and `560001`. A final, separate
yes is needed before Submit.

Spoken answers go to Gnani speech recognition as private turns. They are never read aloud (the box shows
them) and do not enter the command parser, LLM, history or learning store.

The session has questions in ten languages. Detailed field definitions currently cover English and Hindi;
other languages retain visible labels and use English field definitions. Spoken digit words and native
digits are supported, but complex spoken dates and number phrases are intentionally rejected rather than
guessed. Numeric dates use day/month/year or ISO year-month-day.

The initial UIA adapter supports labelled text fields and directly exposed choices. It leaves radio groups,
offscreen fields, unlabelled controls, passwords, OTPs, banking PINs, CAPTCHAs and file uploads to the user.
Skipping any detected field prevents automatic submission. Website identity is not verified: “is this site
real” gives a cautious explanation, never a safety certificate. The adapter cannot prove the completeness
of hidden HTML controls. Review the entire page before agreeing to submit.

The fixture server binds only to `127.0.0.1`, discards submitted bodies, returns no entered values and disables
request logging. It is a test fixture, not a bank or a place to use real KYC details.

The automated form tests use fake UIA controls/backends and local HTTP requests. Browser fixture inspection
is separate from a complete live Bol OS speech-to-browser test, which remains to be performed.
