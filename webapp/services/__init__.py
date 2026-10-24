"""The service layer: plain, typed, documented functions that every page, every API route and (next task) the AI
assistant call. Each takes the database connection and the logged-in `User` first, checks the role, applies the
privacy rules (masking, reveal, audit) and returns the typed models of webapp/models.py."""
