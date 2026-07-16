from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import db
import core
import html
import os
import re
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from logger import get_logger
from url_normalizer import normalise_vinted_url

# Get logger for this module
logger = get_logger(__name__)

# Create Flask app
app = Flask(
    __name__,
    template_folder=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates"
    ),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

# Secret key for session management
app.secret_key = os.urandom(24)


@app.context_processor
def inject_version_info():
    is_up_to_date, current_ver, latest_version, github_url = core.check_version()
    return {
        "github_url": github_url,
        "current_version": current_ver,
        "latest_version": latest_version,
        "is_up_to_date": is_up_to_date,
    }


@app.context_processor
def inject_current_year():
    return {"current_year": datetime.now().year}


def _query_last_found_sort_key(query):
    """Sort newest successful match first, with Never rows at the end."""
    last_item = query[2]

    if last_item is None:
        return (1, 0.0, int(query[0]))

    try:
        timestamp = float(last_item)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid last_item timestamp %r for query %s; treating it as Never.",
            last_item,
            query[0],
        )
        return (1, 0.0, int(query[0]))

    return (0, -timestamp, int(query[0]))


def _queries_newest_first(queries):
    """Return query rows ordered by Last Found Item, descending."""
    return sorted(queries, key=_query_last_found_sort_key)


@app.route("/")
def index():
    # Get parameters
    params = db.get_all_parameters()

    # Get queries, newest Last Found Item first; Never entries last.
    queries = _queries_newest_first(db.get_queries())
    formatted_queries = []
    for i, query in enumerate(queries):
        parsed_query = urlparse(query[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        # last_item is already included in db.get_queries().
        last_timestamp = query[2]
        if last_timestamp is None:
            last_found_item = "Never"
        else:
            try:
                last_found_item = datetime.fromtimestamp(
                    float(last_timestamp)
                ).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError, OverflowError) as error:
                logger.warning(
                    "Could not format last_item %r for query %s: %s",
                    last_timestamp,
                    query[0],
                    error,
                )
                last_found_item = "Never"

        formatted_queries.append(
            {
                "id": i + 1,
                "query_id": query[0],
                "query": query[1],
                "display": query_name if query_name else query[1],
                "last_found_item": last_found_item,
            }
        )

    # Get recent items
    items = db.get_items(limit=10)
    formatted_items = []
    for item in items:
        formatted_items.append(
            {
                "title": item[1],
                "price": item[2],
                "currency": item[3],
                "timestamp": datetime.fromtimestamp(item[4]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "query": item[5],
                "photo_url": item[6],
                "url": f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}/items/{item[0]}",
            }
        )

    # Get process status from the database
    telegram_running = db.get_parameter("telegram_process_running") == "True"
    rss_running = db.get_parameter("rss_process_running") == "True"

    # Get statistics for the dashboard
    stats = {
        "total_items": db.get_total_items_count(),
        "total_queries": db.get_total_queries_count(),
        "items_per_day": db.get_items_per_day(),
    }

    # Get the last found item
    last_item = db.get_last_found_item()
    if last_item:
        stats["last_item"] = {
            "title": last_item[1],
            "price": last_item[2],
            "currency": last_item[3],
            "timestamp": datetime.fromtimestamp(last_item[4]).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "query": last_item[5],
            "photo_url": last_item[6],
            "url": f"{urlparse(last_item[5]).scheme}://{urlparse(last_item[5]).netloc}/items/{last_item[0]}"
        }
    else:
        stats["last_item"] = None

    return render_template(
        "index.html",
        params=params,
        queries=formatted_queries,
        items=formatted_items,
        telegram_running=telegram_running,
        rss_running=rss_running,
        stats=stats,
    )


@app.route("/queries")
def queries():
    # Get queries, newest Last Found Item first; Never entries last.
    all_queries = _queries_newest_first(db.get_queries())
    formatted_queries = []
    for i, query in enumerate(all_queries):
        parsed_query = urlparse(query[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        # last_item is already included in db.get_queries().
        last_timestamp = query[2]
        if last_timestamp is None:
            last_found_item = "Never"
        else:
            try:
                last_found_item = datetime.fromtimestamp(
                    float(last_timestamp)
                ).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError, OverflowError) as error:
                logger.warning(
                    "Could not format last_item %r for query %s: %s",
                    last_timestamp,
                    query[0],
                    error,
                )
                last_found_item = "Never"

        formatted_queries.append(
            {
                "id": i + 1,
                "query_id": query[0],
                "query": query[1],
                "display": query_name if query_name else query[1],
                "last_found_item": last_found_item,
            }
        )

    return render_template("queries.html", queries=formatted_queries)


@app.route("/add_query", methods=["POST"])
def add_query():
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()

    if query:
        try:
            query = normalise_vinted_url(query)
            message, is_new_query = core.process_query(
                query, name=query_name if query_name != "" else None
            )
            if is_new_query:
                flash(f"Query added: {query}", "success")
            else:
                flash(message, "warning")
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
    else:
        flash("No query provided", "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/<int:query_id>", methods=["POST"])
def remove_query(query_id):
    message, success = core.process_remove_query(str(query_id))
    if success:
        flash("Query removed", "success")
    else:
        flash(message, "error")

    return redirect(url_for("queries"))


@app.route("/remove_query/all", methods=["POST"])
def remove_all_queries():
    message, success = core.process_remove_query("all")
    if success:
        flash("All queries removed", "success")
    else:
        flash(message, "error")

    return redirect(url_for("queries"))


@app.route("/update_query/<int:query_id>", methods=["POST"])
def update_query(query_id):
    query = request.form.get("query")
    query_name = request.form.get("query_name", "").strip()

    if query:
        try:
            query = normalise_vinted_url(query)
            message, success = core.process_update_query(
                query_id, query, name=query_name if query_name != "" else None
            )
            if success:
                flash("Query updated", "success")
            else:
                flash(message, "error")
        except (ValueError, TypeError) as error:
            flash(str(error), "error")
    else:
        flash("No query provided", "error")

    return redirect(url_for("queries"))


@app.route("/items")
def items():
    query_id = request.args.get("query", "")  # Default to empty string instead of None
    limit = int(request.args.get("limit", 50))

    # Get items
    query_string = None
    if query_id:
        # Get the actual query string for the given ID
        queries = db.get_queries()
        for q in queries:
            if str(q[0]) == query_id:
                query_string = q[1]
                break

    items_data = db.get_items(limit=limit, query=query_string)
    formatted_items = []

    for item in items_data:
        formatted_items.append(
            {
                "title": item[1],
                "price": item[2],
                "currency": item[3],
                "timestamp": datetime.fromtimestamp(item[4]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                # Ugly Ugly Ugly very Ugly eeew but I have to do a proper migration of existing db later else it'll break
                # Eeew bad me >:c
                "query": (
                    item[7] if item[7] else parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
                    if parse_qs(urlparse(item[5]).query).get("search_text", [None])[0]
                    else item[5]
                ),
                "url": f"{urlparse(item[5]).scheme}://{urlparse(item[5]).netloc}/items/{item[0]}",
                "photo_url": item[6],
            }
        )

    # Get queries for filter dropdown
    queries = db.get_queries()
    formatted_queries = []
    selected_query_display = None
    for i, q in enumerate(queries):
        parsed_query = urlparse(q[1])
        query_params = parse_qs(parsed_query.query)
        query_name = (
            q[3] if q[3] is not None else query_params.get("search_text", [None])[0]
        )
        display_name = query_name if query_name else q[0]
        # Store display name for selected query
        if query_id == str(q[0]):
            selected_query_display = display_name
        formatted_queries.append(
            {"id": i + 1, "query_id": q[0], "query": q[1], "display": display_name}
        )

    return render_template(
        "items.html",
        items=formatted_items,
        queries=formatted_queries,
        selected_query=query_id,
        selected_query_display=selected_query_display,
        limit=limit,
    )


def _quiet_hours_panel(params):
    """Render quiet-hours controls inside the existing configuration form."""
    enabled = params.get("quiet_hours_enabled", "True") == "True"
    start = html.escape(params.get("quiet_hours_start", "01:00"), quote=True)
    end = html.escape(params.get("quiet_hours_end", "06:00"), quote=True)
    timezone_name = html.escape(
        params.get("quiet_hours_timezone", "Europe/London"),
        quote=True,
    )
    checked = "checked" if enabled else ""

    return f"""
    <div id="quiet-hours-settings" class="card mb-4">
        <div class="card-header">
            <strong>Quiet hours</strong>
        </div>
        <div class="card-body">
            <div class="form-check form-switch mb-3">
                <input class="form-check-input" type="checkbox"
                       id="quiet_hours_enabled" name="quiet_hours_enabled" {checked}>
                <label class="form-check-label" for="quiet_hours_enabled">
                    Pause Vinted scraping during quiet hours
                </label>
            </div>
            <div class="row g-3">
                <div class="col-md-4">
                    <label for="quiet_hours_start" class="form-label">Start</label>
                    <input type="time" class="form-control" id="quiet_hours_start"
                           name="quiet_hours_start" value="{start}" required>
                </div>
                <div class="col-md-4">
                    <label for="quiet_hours_end" class="form-label">End</label>
                    <input type="time" class="form-control" id="quiet_hours_end"
                           name="quiet_hours_end" value="{end}" required>
                </div>
                <div class="col-md-4">
                    <label for="quiet_hours_timezone" class="form-label">Timezone</label>
                    <input type="text" class="form-control"
                           id="quiet_hours_timezone" name="quiet_hours_timezone"
                           value="{timezone_name}" required>
                </div>
            </div>
            <div class="form-text mt-2">
                Use an IANA timezone such as Europe/London. This remains correct when
                the app runs on a UTC server and follows BST/GMT automatically. The
                global quiet period applies to every Telegram account. Windows that
                cross midnight are supported.
            </div>
            <button type="submit" class="btn btn-primary mt-3">
                Save Configuration
            </button>
        </div>
    </div>
    """


@app.route("/config")
def config():
    params = db.get_all_parameters()
    rendered = render_template("config.html", params=params)

    if 'id="quiet-hours-settings"' not in rendered and "</form>" in rendered:
        before, closing = rendered.rsplit("</form>", 1)
        rendered = before + _quiet_hours_panel(params) + "</form>" + closing

    return rendered


@app.route("/update_config", methods=["POST"])
def update_config():
    quiet_start = request.form.get("quiet_hours_start", "01:00").strip()
    quiet_end = request.form.get("quiet_hours_end", "06:00").strip()
    quiet_timezone = (
        request.form.get("quiet_hours_timezone", "Europe/London").strip()
        or "Europe/London"
    )

    try:
        datetime.strptime(quiet_start, "%H:%M")
        datetime.strptime(quiet_end, "%H:%M")
    except ValueError:
        flash("Quiet-hours times must be valid 24-hour times.", "error")
        return redirect(url_for("config"))

    if quiet_start == quiet_end:
        flash("Quiet-hours start and end times must be different.", "error")
        return redirect(url_for("config"))

    try:
        ZoneInfo(quiet_timezone)
    except ZoneInfoNotFoundError:
        flash(
            "Quiet-hours timezone is not recognised. Use an IANA name such as "
            "Europe/London.",
            "error",
        )
        return redirect(url_for("config"))

    # Update Telegram parameters
    telegram_enabled = "telegram_enabled" in request.form
    db.set_parameter("telegram_enabled", str(telegram_enabled))
    db.set_parameter("telegram_token", request.form.get("telegram_token", ""))
    db.set_parameter("telegram_chat_id", request.form.get("telegram_chat_id", ""))

    # Update RSS parameters
    rss_enabled = "rss_enabled" in request.form
    db.set_parameter("rss_enabled", str(rss_enabled))
    db.set_parameter("rss_port", request.form.get("rss_port", "8080"))
    db.set_parameter("rss_max_items", request.form.get("rss_max_items", "100"))

    # Update System parameters
    db.set_parameter("items_per_query", request.form.get("items_per_query", "20"))
    db.set_parameter(
        "query_refresh_delay", request.form.get("query_refresh_delay", "60")
    )
    db.set_parameter("banwords", request.form.get("banwords", ""))

    # Update quiet-hours parameters
    quiet_enabled = "quiet_hours_enabled" in request.form
    db.set_parameter("quiet_hours_enabled", str(quiet_enabled))
    db.set_parameter("quiet_hours_start", quiet_start)
    db.set_parameter("quiet_hours_end", quiet_end)
    db.set_parameter("quiet_hours_timezone", quiet_timezone)

    # Update Proxy parameters
    check_proxies = "check_proxies" in request.form
    db.set_parameter("check_proxies", str(check_proxies))
    db.set_parameter("proxy_list", request.form.get("proxy_list", ""))
    db.set_parameter("proxy_list_link", request.form.get("proxy_list_link", ""))

    # Update Advanced parameters
    db.set_parameter("message_template", request.form.get("message_template", ""))
    db.set_parameter("user_agents", request.form.get("user_agents", "[]"))
    db.set_parameter("default_headers", request.form.get("default_headers", "{}"))

    # Reset proxy cache to force refresh on next use
    db.set_parameter("last_proxy_check_time", "1")
    logger.info("Proxy settings updated, cache reset")

    flash("Configuration updated", "success")
    return redirect(url_for("config"))


@app.route("/control/<process_name>/<action>", methods=["POST"])
def control_process(process_name, action):
    if process_name not in ["telegram", "rss"]:
        return jsonify({"status": "error", "message": "Invalid process name"})

    if action == "start":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": "Telegram bot already running"}
                )

            # Check if telegram_token and telegram_chat_id are set
            telegram_token = db.get_parameter("telegram_token")
            telegram_chat_id = db.get_parameter("telegram_chat_id")
            if not telegram_token or not telegram_chat_id:
                return jsonify(
                    {
                        "status": "error",
                        "message": "Please set Telegram token and chat ID in the configuration panel before starting the Telegram process",
                    }
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("telegram_process_running", "True")
            logger.info("Telegram bot process start requested")
            return jsonify(
                {"status": "success", "message": "Telegram bot start requested"}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") == "True":
                return jsonify(
                    {"status": "warning", "message": "RSS feed already running"}
                )

            # Update process status in the database
            # The manager process will detect this and start the process
            db.set_parameter("rss_process_running", "True")
            logger.info("RSS feed process start requested")
            return jsonify({"status": "success", "message": "RSS feed start requested"})

    elif action == "stop":
        if process_name == "telegram":
            # Check current status
            if db.get_parameter("telegram_process_running") != "True":
                return jsonify(
                    {"status": "warning", "message": "Telegram bot not running"}
                )

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("telegram_process_running", "False")
            logger.info("Telegram bot process stop requested")
            return jsonify(
                {"status": "success", "message": "Telegram bot stop requested"}
            )

        elif process_name == "rss":
            # Check current status
            if db.get_parameter("rss_process_running") != "True":
                return jsonify({"status": "warning", "message": "RSS feed not running"})

            # Update process status in the database
            # The manager process will detect this and stop the process
            db.set_parameter("rss_process_running", "False")
            logger.info("RSS feed process stop requested")
            return jsonify({"status": "success", "message": "RSS feed stop requested"})

    return jsonify({"status": "error", "message": "Invalid action"})


@app.route("/control/status", methods=["GET"])
def process_status():
    # Get process status from the database
    telegram_running = db.get_parameter("telegram_process_running") == "True"
    rss_running = db.get_parameter("rss_process_running") == "True"

    return jsonify({"telegram": telegram_running, "rss": rss_running})


@app.route("/allowlist")
def allowlist():
    countries = db.get_allowlist()
    if countries == 0:
        countries = []

    return render_template("allowlist.html", countries=countries)


@app.route("/add_country", methods=["POST"])
def add_country():
    country = request.form.get("country", "").strip()
    if country:
        message, country_list = core.process_add_country(country)
        flash(message, "success" if "added" in message else "warning")
    else:
        flash("No country provided", "error")

    return redirect(url_for("allowlist"))


@app.route("/remove_country/<country>", methods=["POST"])
def remove_country(country):
    message, country_list = core.process_remove_country(country)
    flash(message, "success")

    return redirect(url_for("allowlist"))


@app.route("/clear_allowlist", methods=["POST"])
def clear_allowlist():
    db.clear_allowlist()
    flash("Allowlist cleared", "success")

    return redirect(url_for("allowlist"))


@app.route("/logs")
def logs():
    return render_template("logs.html")


@app.route("/api/logs")
def api_logs():
    offset = int(request.args.get("offset", 0))
    limit = int(request.args.get("limit", 100))
    level_filter = request.args.get("level", "all")

    log_file_path = os.path.join("logs", "vinted.log")

    if not os.path.exists(log_file_path):
        return jsonify({"logs": [], "total": 0})

    # Parse log file
    log_entries = []
    total_matching_entries = 0

    try:
        with open(log_file_path, "r", encoding="utf-8") as file:
            # Read all lines from the file
            all_lines = file.readlines()

            # Process lines in reverse order (newest first)
            all_lines.reverse()

            # Regular expression to parse log lines
            # Format: 2023-09-15 12:34:56,789 - module_name - LEVEL - Message
            log_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - ([^-]+) - ([A-Z]+) - (.+)"

            current_entry = 0

            for line in all_lines:
                match = re.match(log_pattern, line.strip())
                if match:
                    timestamp, module, level, message = match.groups()

                    # Apply level filter if specified
                    if level_filter != "all" and level != level_filter:
                        continue

                    total_matching_entries += 1

                    # Skip entries before offset
                    if total_matching_entries <= offset:
                        continue

                    # Add entry if within limit
                    if current_entry < limit:
                        log_entries.append(
                            {
                                "timestamp": timestamp,
                                "module": module.strip(),
                                "level": level,
                                "message": message,
                            }
                        )
                        current_entry += 1

                    # Stop if we've reached the limit
                    if current_entry >= limit:
                        break
    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        return jsonify({"logs": [], "total": 0, "error": str(e)})

    return jsonify({"logs": log_entries, "total": total_matching_entries})


def web_ui_process():
    logger.info("Web UI process started")
    try:
        app.run(host="0.0.0.0", port=8000, debug=False)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Web UI process stopped")
    except Exception as e:
        logger.error(f"Error in web UI process: {e}", exc_info=True)
