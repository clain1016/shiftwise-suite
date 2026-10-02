"""ShiftWise routes and blueprint registration."""

from shiftwise.routes.auth import auth_bp
from shiftwise.routes.calendar import calendar_bp
from shiftwise.routes.conflicts import conflicts_bp
from shiftwise.routes.employee import employee_bp
from shiftwise.routes.manager import manager_bp
from shiftwise.routes.roster import roster_bp


def register_blueprints(app):
    """Register all modular blueprints and setup backward-compatible URL endpoints."""
    blueprints = [
        auth_bp,
        calendar_bp,
        conflicts_bp,
        employee_bp,
        manager_bp,
        roster_bp,
    ]
    for bp in blueprints:
        app.register_blueprint(bp)

    # Establish root endpoint aliases so url_for('name') matches url_for('bp.name')
    for rule in list(app.url_map.iter_rules()):
        if "." in rule.endpoint:
            short = rule.endpoint.split(".", 1)[1]
            if short not in app.view_functions:
                app.add_url_rule(
                    rule.rule,
                    endpoint=short,
                    view_func=app.view_functions[rule.endpoint],
                    methods=rule.methods,
                )


__all__ = [
    "auth_bp",
    "calendar_bp",
    "conflicts_bp",
    "employee_bp",
    "manager_bp",
    "roster_bp",
    "register_blueprints",
]
