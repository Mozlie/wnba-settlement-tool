from flask import Flask, render_template, request, send_file, flash, redirect, url_for
import pandas as pd
import re
import os
import tempfile

app = Flask(__name__)
app.secret_key = "wnba_settlement_secret_key"

# --- Helper Functions ---

def normalize_name(name):
    if not isinstance(name, str):
        return ""
    return re.sub(r'[^a-z0-9]', '', name.lower())

def parse_espn_box_score_final(text):
    players_data = {}
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    player_names = []
    stat_lines = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Detect new team and flush previous block
        if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*$', line) and i + 1 < len(lines) and 'starters' in lines[i + 1].lower():
            num_to_process = min(len(player_names), len(stat_lines))
            for j in range(num_to_process):
                try:
                    pname = normalize_name(player_names[j])
                    stats = stat_lines[j].split()
                    players_data[pname] = {
                        'points': int(stats[-1]),
                        'status': 'active'
                    }
                    print(f"SUCCESS: Parsed '{player_names[j]}' -> {players_data[pname]}")
                except Exception as e:
                    print(f"WARNING: Failed parsing '{player_names[j]}': {stat_lines[j]} — {e}")
            player_names = []
            stat_lines = []
            i += 1
            continue

        # Player + jersey number parsing
        if i + 1 < len(lines) and re.match(r'^#?\d+$', lines[i + 1].strip()):
            name = lines[i].strip()
            if i + 2 < len(lines) and ("DNP" in lines[i + 2] or "NWT" in lines[i + 2]):
                players_data[normalize_name(name)] = {'status': 'DNP', 'points': 0}
                print(f"SUCCESS: Parsed '{name}' as DNP.")
                i += 3
            else:
                player_names.append(name)
                i += 2
        elif re.match(r'^\d{1,2}\s+\d{1,2}-\d{1,2}', line):
            stat_lines.append(line)
            i += 1
        else:
            i += 1

    # Final flush
    num_to_process = min(len(player_names), len(stat_lines))
    for j in range(num_to_process):
        try:
            pname = normalize_name(player_names[j])
            stats = stat_lines[j].split()
            players_data[pname] = {
                'points': int(stats[-1]),
                'status': 'active'
            }
            print(f"SUCCESS: Parsed '{player_names[j]}' -> {players_data[pname]}")
        except Exception as e:
            print(f"WARNING: Failed parsing '{player_names[j]}': {stat_lines[j]} — {e}")

    print("Parsed players:", list(players_data.keys()))
    return players_data

def parse_single_player_points_market(outcome_name):
    pattern = re.compile(r'\[(.+?)\s+(Over|Under)\s+([\d.]+)\s+Points\]', re.IGNORECASE)
    match = pattern.search(outcome_name)
    if match:
        return [{
            'player': normalize_name(match.group(1).strip()),
            'condition': match.group(2).lower(),
            'value': float(match.group(3))
        }]
    return None

def parse_multi_player_points_market(outcome_name):
    pattern = re.compile(r'\[Both To Score (\d+)\+ Points\]', re.IGNORECASE)
    match = pattern.search(outcome_name)
    if not match:
        return None

    value = int(match.group(1))
    player_names = re.findall(r'\[([^\]]+)\]', outcome_name)
    non_players = ['and', 'ny', 'lv', 'sea', 'ct', 'chi', 'dal', 'ind', 'atl', 'la', 'min', 'phx', 'wsh']

    conditions = []
    for name in player_names:
        if not any(char.isdigit() for char in name) and normalize_name(name) not in non_players:
            conditions.append({
                'player': normalize_name(name),
                'condition': 'over',
                'value': value
            })

    return conditions if conditions else None

# --- Routes ---

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    if 'events_file' not in request.files:
        flash("No Events Result file was uploaded.", "error")
        return redirect(url_for('index'))
    events_file = request.files['events_file']
    if events_file.filename == '':
        flash("No file was selected for Events Result.", "error")
        return redirect(url_for('index'))

    try:
        df_events = pd.read_csv(events_file, dtype=str)
        required_cols = ['outcome_id', 'outcome_name', 'market_name']
        if not all(col in df_events.columns for col in required_cols):
            flash(f"Events file missing required columns: {required_cols}", "error")
            return redirect(url_for('index'))
    except Exception as e:
        flash(f"Error reading events file: {e}", "error")
        return redirect(url_for('index'))

    box_score_text = request.form.get('box_score')
    filename = request.form.get('filename', 'settlement.txt')
    if not filename.lower().endswith('.txt'):
        filename += '.txt'
    if not box_score_text:
        flash("Box score text is empty.", "error")
        return redirect(url_for('index'))

    player_data = parse_espn_box_score_final(box_score_text)
    if not player_data:
        flash("Could not parse any player stats from the pasted text. The format might not be from ESPN or has changed.", "error")
        return redirect(url_for('index'))

    settlement_data = []
    display_log = []

    points_markets = df_events[df_events['market_name'].str.lower().str.contains('points', na=False)].copy()

    for index, row in points_markets.iterrows():
        outcome_id = row['outcome_id']
        outcome_name = row['outcome_name']
        market_conditions = parse_multi_player_points_market(outcome_name) or parse_single_player_points_market(outcome_name)

        if not market_conditions:
            continue

        result_cname = ''
        log_details = []
        is_void = False

        for condition in market_conditions:
            player = condition['player']
            if player not in player_data:
                print(f"DEBUG: Player '{player}' not found in parsed player_data. Keys: {list(player_data.keys())}")
            if player not in player_data or player_data[player]['status'] == 'DNP':
                is_void = True
                log_details.append(f"Player '{player}' did not play.")
                break

        if is_void:
            result_cname = 'void'
        else:
            all_legs_win = True
            for condition in market_conditions:
                player = condition['player']
                cond_type = condition['condition']
                value = condition['value']
                actual_points = player_data[player]['points']
                leg_result = (actual_points >= value) if cond_type == 'over' else (actual_points < value)
                if not leg_result:
                    all_legs_win = False
                    log_details.append(f"Player '{player}' failed (Actual: {actual_points})")

            result_cname = 'win' if all_legs_win else 'loss'
            if len(market_conditions) == 1:
                single_player = market_conditions[0]['player']
                if single_player in player_data and player_data[single_player]['points'] == market_conditions[0]['value']:
                    result_cname = 'push'

        settlement_data.append({
            'outcome_id': outcome_id,
            'result_cname': result_cname,
            'handicap': '0',
            'handicap_operator': '='
        })

        log_message = f"Settled '{outcome_name}' as {result_cname.upper()}"
        if log_details:
            log_message += f" - Reason: {'; '.join(log_details)}"
        display_log.append(log_message)

    if not settlement_data:
        flash("No 'points' markets were found or settled.", "warning")
        return redirect(url_for('index'))

    df_settlement = pd.DataFrame(settlement_data)
    try:
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, filename)
        df_settlement.to_csv(temp_path, index=False, sep='\t', columns=['outcome_id', 'result_cname', 'handicap', 'handicap_operator'])
    except Exception as e:
        flash(f"Failed to write settlement file: {e}", "error")
        return redirect(url_for('index'))

    flash(f"Successfully settled {len(settlement_data)} markets!", "success")
    return render_template('index.html', results=display_log, filepath=temp_path, filename=filename)

@app.route('/download')
def download():
    filepath = request.args.get('filepath')
    filename = request.args.get('filename')
    if not filepath or not os.path.exists(filepath):
        flash("File not found or has expired. Please process again.", "error")
        return redirect(url_for('index'))
    try:
        return send_file(filepath, mimetype='text/plain', as_attachment=True, download_name=filename)
    except Exception as e:
        flash(f"Download failed: {e}", "error")
        return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
r
