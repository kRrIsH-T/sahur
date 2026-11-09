"""Private, local form guidance: explain a form, fill it by voice, check the site, keep the user's own details.

Entry points: FormAssistant (pipeline actions), FormSession (the filling dialogue), explain.explain,
safety.assess, profile.Profile, validate.check, intent / fraud_report / forget_request (local routing).
Spoken answers use Gnani recognition.
"""

from bolos.forms.intent import forget_request, fraud_report, intent
from bolos.forms.session import FormAssistant, FormSession, SessionResult

__all__ = ["FormAssistant", "FormSession", "SessionResult", "forget_request", "fraud_report", "intent"]
