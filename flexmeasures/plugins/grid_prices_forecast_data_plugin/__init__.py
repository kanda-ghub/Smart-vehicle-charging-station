from flask import Blueprint

# 1. Create a Blueprint for the plugin (FlexMeasures looks for <plugin_name>_bp)
grid_prices_forecast_data_plugin_bp = Blueprint(
    "grid_prices_forecast_data_plugin",
    __name__,
    template_folder="templates",
    static_folder="static",
)

# 2. Import your reporter so it gets registered with FlexMeasures
from grid_prices_forecast_data_plugin.grid_prices_reporter import EntsoePriceReporter
