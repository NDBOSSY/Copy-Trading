from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime
import threading
import time
import json
import logging
import os
from typing import Dict, List

# Configuration
API_KEY = os.environ.get("COPY_API_KEY", "change_me_secret")
PORT = int(os.environ.get("PORT", 8000))

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# In-memory storage (for Railway, consider Redis for production)
connected_accounts = {}
recent_signals = []
master_account = None
accounts_lock = threading.Lock()
signals_lock = threading.Lock()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Background cleanup thread
def cleanup_stale_connections():
    """Background thread to clean up stale connections every minute"""
    global master_account, connected_accounts
    
    while True:
        try:
            time.sleep(60)  # Run every minute
            
            with accounts_lock:
                current_time = datetime.now()
                stale_accounts = []
                
                for acc_id, acc in connected_accounts.items():
                    last_seen = datetime.fromisoformat(acc['last_seen'])
                    if (current_time - last_seen).total_seconds() > 300:  # 5 minutes
                        stale_accounts.append(acc_id)
                
                for acc_id in stale_accounts:
                    del connected_accounts[acc_id]
                    # Only clear master_account if the disconnected account was actually the master
                    if master_account and acc_id == master_account:
                        master_account = None
                
                if stale_accounts:
                    logger.info(f"Cleaned up {len(stale_accounts)} stale connections: {stale_accounts}")
                    
        except Exception as e:
            logger.error(f"Error in cleanup thread: {e}")

# Start the cleanup thread when server starts
cleanup_thread = threading.Thread(target=cleanup_stale_connections, daemon=True)
cleanup_thread.start()

def validate_api_key():
    """Validate API key from request headers"""
    api_key = request.headers.get('x-api-key')
    return api_key == API_KEY

# Signal Endpoints
@app.route('/signal', methods=['POST'])
def handle_signal():
    """Receive trading signals from master"""
    if not validate_api_key():
        return jsonify({'error': 'Invalid API key'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # Add timestamp and signal ID - SAFELY handle master_account
        signal_data = {
            'signal_id': f"sig_{int(time.time())}_{len(recent_signals)}",
            'timestamp': time.time(),
            'master_account': master_account if master_account else 'no_master',
            **data
        }
        
        with signals_lock:
            recent_signals.append(signal_data)
            # Keep only last 1000 signals
            if len(recent_signals) > 1000:
                recent_signals.pop(0)
        
        logger.info(f"Signal received: {data.get('action')} {data.get('symbol')}")
        
        return jsonify({
            'status': 'success',
            'signal_id': signal_data['signal_id'],
            'message': 'Signal processed'
        }), 200
        
    except Exception as e:
        logger.error(f"Signal error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/signals', methods=['GET'])
def get_signals():
    """Get recent signals"""
    try:
        hours = request.args.get('hours', 24, type=int)
        cutoff_time = time.time() - (hours * 3600)
        
        with signals_lock:
            filtered_signals = [s for s in recent_signals if s['timestamp'] >= cutoff_time]
        
        return jsonify({
            'signals': filtered_signals,
            'count': len(filtered_signals)
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Account Management
@app.route('/register', methods=['POST'])
def register_account():
    """Register master or slave account"""
    global master_account
    
    try:
        data = request.get_json()
        account_id = data.get('account_id')
        name = data.get('name', 'Unknown')
        is_master = data.get('is_master', False)
        
        if not account_id:
            return jsonify({'error': 'Account ID required'}), 400
        
        account_data = {
            'account_id': account_id,
            'name': name,
            'is_master': is_master,
            'connected_since': datetime.now().isoformat(),
            'last_seen': datetime.now().isoformat(),
            'equity': data.get('equity', 0),
            'profit': data.get('profit', 0),
            'status': 'connected',
            'ip_address': request.remote_addr,
            # STORE LICENSE DATA FROM SLAVE REGISTRATION
            'license_owner': data.get('license_owner'),
            'license_key': data.get('license_key')
        }
        
        with accounts_lock:
            if is_master:
                master_account = account_id
                logger.info(f"MASTER registered: {name} (ID: {account_id})")
            else:
                logger.info(f"SLAVE registered: {name} (ID: {account_id}) - License: {data.get('license_owner', 'None')}")
            
            connected_accounts[account_id] = account_data
        
        # DEBUG: Log current state after registration
        logger.info(f"DEBUG - Total accounts after registration: {len(connected_accounts)}")
        logger.info(f"DEBUG - Account IDs: {list(connected_accounts.keys())}")
        
        return jsonify({
            'status': 'success',
            'account_id': account_id,
            'is_master': is_master
        }), 200
        
    except Exception as e:
        logger.error(f"Registration error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/connected-accounts', methods=['GET'])
def get_connected_accounts():
    """Get all connected accounts - FIXED: No cleanup during fetch"""
    global master_account
    
    try:
        # REMOVED cleanup logic - it runs in background thread now
        # Just return the current state without modifying it
        
        with accounts_lock:
            accounts_copy = connected_accounts.copy()
        
        # DEBUG: Log what we're returning
        slave_count = sum(1 for acc in accounts_copy.values() if not acc.get('is_master', False))
        master_count = sum(1 for acc in accounts_copy.values() if acc.get('is_master', False))
        
        logger.info(f"DEBUG - Returning {len(accounts_copy)} accounts ({master_count} masters, {slave_count} slaves)")
        
        return jsonify({
            'accounts': accounts_copy,
            'total_count': len(accounts_copy),
            'master_account': master_account,
            'timestamp': time.time()
        }), 200
        
    except Exception as e:
        logger.error(f"Error in connected-accounts: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    """Update account heartbeat"""
    try:
        data = request.get_json()
        account_id = data.get('account_id')
        
        if not account_id:
            return jsonify({'error': 'Account ID required'}), 400
        
        with accounts_lock:
            if account_id in connected_accounts:
                connected_accounts[account_id]['last_seen'] = datetime.now().isoformat()
                connected_accounts[account_id]['equity'] = data.get('equity', connected_accounts[account_id]['equity'])
                connected_accounts[account_id]['profit'] = data.get('profit', connected_accounts[account_id]['profit'])
                connected_accounts[account_id]['status'] = 'connected'
                # License data persists automatically - no need to update
                
                # DEBUG: Log successful heartbeat
                logger.debug(f"Heartbeat from {account_id}")
            else:
                logger.warning(f"Heartbeat from unknown account: {account_id}")
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/disconnect', methods=['POST'])
def disconnect():
    """Handle account disconnect"""
    global master_account
    
    try:
        data = request.get_json()
        account_id = data.get('account_id')
        
        if not account_id:
            return jsonify({'error': 'Account ID required'}), 400
        
        with accounts_lock:
            if account_id in connected_accounts:
                account_name = connected_accounts[account_id].get('name', 'Unknown')
                is_master = connected_accounts[account_id].get('is_master', False)
                
                if is_master:
                    logger.info(f"MASTER disconnected: {account_name} (ID: {account_id})")
                else:
                    logger.info(f"SLAVE disconnected: {account_name} (ID: {account_id})")
                
                connected_accounts[account_id]['status'] = 'disconnected'
                if account_id == master_account:
                    master_account = None
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Master Control
@app.route('/master/status', methods=['GET'])
def get_master_status():
    """Get master account status"""
    try:
        master_data = None
        if master_account and master_account in connected_accounts:
            master_data = connected_accounts[master_account]
        
        return jsonify({
            'master_account': master_data,
            'has_master': master_data is not None
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Health check
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    with accounts_lock:
        accounts_count = len(connected_accounts)
        slave_count = sum(1 for acc in connected_accounts.values() if not acc.get('is_master', False))
        master_count = sum(1 for acc in connected_accounts.values() if acc.get('is_master', False))
    
    return jsonify({
        'status': 'healthy',
        'timestamp': time.time(),
        'accounts_count': accounts_count,
        'slave_count': slave_count,
        'master_count': master_count,
        'signals_count': len(recent_signals),
        'master_online': master_account is not None
    }), 200

@app.route('/debug/accounts', methods=['GET'])
def debug_accounts():
    """Debug endpoint to see all accounts in detail"""
    with accounts_lock:
        accounts_copy = connected_accounts.copy()
    
    return jsonify({
        'accounts': accounts_copy,
        'total_count': len(accounts_copy),
        'master_account': master_account,
        'timestamp': time.time()
    }), 200

@app.route('/')
def home():
    """Simple home page"""
    return jsonify({
        'message': 'Copy Trading Server is running',
        'version': '1.0.0',
        'endpoints': {
            'signal': 'POST /signal',
            'register': 'POST /register',
            'accounts': 'GET /connected-accounts',
            'health': 'GET /health',
            'debug': 'GET /debug/accounts'
        }
    })

if __name__ == '__main__':
    logger.info(f"Starting Copy Trading Server on port {PORT}")
    logger.info("Stale connection cleanup running in background thread")
    app.run(host='0.0.0.0', port=PORT, debug=False)
