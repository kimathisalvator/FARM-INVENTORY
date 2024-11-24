# app.py
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort, send_file, make_response
import pymysql
import re
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from flask import session, redirect, url_for, flash
from datetime import datetime
import csv
from io import StringIO
app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Change this to a secure secret key

# Database connection
def get_db_connection():
    return pymysql.connect(
        host='localhost',
        user='root',
        password='',  # Add your MySQL password
        database='farm_inventory_system',
        cursorclass=pymysql.cursors.DictCursor
    )

def role_required(roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'loggedin' not in session:
                flash('Please log in first')
                return redirect(url_for('login'))
            
            if session['role'] not in roles:
                flash('You do not have permission to access this page')
                # Redirect to the appropriate page based on user's role
                role_redirects = {
                    'admin': 'admin',
                    'employee': 'dashboard',
                    'manager': 'manager',
                    'vet': 'vet'
                }
                return redirect(url_for(role_redirects.get(session['role'], 'login')))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def check_admin():
    """Helper function to check if user is logged in as admin"""
    return 'loggedin' in session and session.get('role') == 'admin'

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not check_admin():
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/')
def index():
    return redirect(url_for('login'))

# Then you could use it like this:
@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html')

@app.route('/dashboard')
def dashboard():
    if 'loggedin' not in session:
        return redirect(url_for('login'))
    return render_template('dashboard.html')

@app.route('/manager', methods=['GET'])
def manager():
    if 'loggedin' not in session:
        return redirect(url_for('login'))
    
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Fetch stocks with their current quantities and latest transactions
            query = """
            SELECT 
                s.*,
                sq.quantity,
                sq.last_updated as quantity_updated,
                COALESCE(
                    (SELECT AVG(unit_price) 
                     FROM stock_transactions 
                     WHERE stock_id = s.id AND transaction_type = 'in'
                    ), 0
                ) as avg_price,
                COALESCE(
                    (SELECT supplier 
                     FROM stock_transactions 
                     WHERE stock_id = s.id AND transaction_type = 'in'
                     ORDER BY transaction_date DESC 
                     LIMIT 1
                    ), ''
                ) as last_supplier
            FROM stocks s
            LEFT JOIN stock_quantity sq ON s.id = sq.stock_id
            ORDER BY s.category, s.name
            """
            cursor.execute(query)
            inventory = cursor.fetchall()
            
            # Fetch recent transactions
            cursor.execute("""
                SELECT 
                    t.*,
                    s.name as stock_name,
                    s.measure
                FROM stock_transactions t
                JOIN stocks s ON t.stock_id = s.id
                ORDER BY t.transaction_date DESC
                LIMIT 10
            """)
            recent_transactions = cursor.fetchall()
        
        return render_template('manager.html', 
                             inventory=inventory,
                             recent_transactions=recent_transactions)
        
    except Exception as e:
        return f"Error: {str(e)}"
        
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/stock/transaction', methods=['POST'])
def stock_transaction():
    if 'loggedin' not in session:
        return redirect(url_for('login'))
    
    stock_id = request.form.get('stock_id')
    quantity = float(request.form.get('quantity', 0))
    transaction_type = request.form.get('transaction_type')
    unit_price = float(request.form.get('unit_price', 0)) if transaction_type == 'in' else None
    supplier = request.form.get('supplier') if transaction_type == 'in' else None
    purpose = request.form.get('purpose')
    
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Start transaction
            conn.begin()
            
            # First, check if stock exists in stock_quantity
            cursor.execute("""
                SELECT quantity 
                FROM stock_quantity 
                WHERE stock_id = %s
            """, (stock_id,))
            current_stock = cursor.fetchone()
            
            # Insert transaction record
            cursor.execute("""
                INSERT INTO stock_transactions 
                (stock_id, transaction_type, quantity, unit_price, purpose, supplier, 
                 transaction_date, recorded_by)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s)
            """, (stock_id, transaction_type, quantity, unit_price, purpose, supplier, 
                  session['user_id']))

            # Update stock quantity based on transaction type
            if transaction_type == 'in':
                if current_stock:
                    # Update existing stock
                    cursor.execute("""
                        UPDATE stock_quantity 
                        SET quantity = quantity + %s,
                            last_updated = NOW(),
                            last_updated_by = %s
                        WHERE stock_id = %s
                    """, (quantity, session['user_id'], stock_id))
                else:
                    # Insert new stock record
                    cursor.execute("""
                        INSERT INTO stock_quantity 
                        (stock_id, quantity, last_updated, last_updated_by)
                        VALUES (%s, %s, NOW(), %s)
                    """, (stock_id, quantity, session['user_id']))
            else:  # transaction_type == 'out'
                if not current_stock or current_stock['quantity'] < quantity:
                    raise Exception("Insufficient stock quantity")
                
                # Update stock quantity
                cursor.execute("""
                    UPDATE stock_quantity
                    SET quantity = quantity - %s,
                        last_updated = NOW(),
                        last_updated_by = %s
                    WHERE stock_id = %s
                """, (quantity, session['user_id'], stock_id))
            
            conn.commit()
            flash('Transaction recorded successfully!', 'success')
            
        return redirect(url_for('manager'))
        
    except Exception as e:
        if 'conn' in locals():
            conn.rollback()
        flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('manager'))
        
    finally:
        if 'conn' in locals():
            conn.close()


@app.route('/vet')
def vet():
    if 'loggedin' not in session or session['role'] != 'vet':
        return redirect(url_for('login'))
    return render_template('vet.html')

@app.route('/hr-management')
def hr_management():
    if not check_admin():
        return redirect(url_for('login'))
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Fetch all users with their status and role
        cur.execute('''
            SELECT id, name, email, phone, code, created_at, status, role 
            FROM users 
            ORDER BY created_at DESC
        ''')
        users = cur.fetchall()
        
        return render_template('hr_management.html', users=users)
        
    except Exception as e:
        flash('An error occurred while fetching user data', 'error')
        return render_template('hr_management.html', users=[])
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()


@app.route('/update-user/<int:user_id>', methods=['POST'])
def update_user(user_id):
    print("Session contents (update-user):", session)
    if 'loggedin' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 403
    if 'role' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Not admin'}), 403
    
    try:
        data = request.get_json()
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if user exists
        cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        print("User data from DB:", user)
        
        if not user:
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Update user details
        cur.execute('''
            UPDATE users 
            SET name = %s, email = %s, phone = %s, role = %s 
            WHERE id = %s
        ''', (data['name'], data['email'], data['phone'], data['role'], user_id))
        
        conn.commit()
        return jsonify({'success': True, 'message': 'User updated successfully'})
        
    except Exception as e:
        print(f"Error in update-user: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/toggle-status/<int:user_id>', methods=['POST'])
def toggle_status(user_id):
    print("Session contents (toggle-status):", session)
    if 'loggedin' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 403
    if 'role' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Not admin'}), 403
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if user exists and get current status
        cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        print("User data from DB:", user)
        
        if not user:
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Toggle status
        new_status = 'suspended' if user['status'] == 'active' else 'active'
        cur.execute('UPDATE users SET status = %s WHERE id = %s', (new_status, user_id))
        
        conn.commit()
        return jsonify({'success': True, 'message': f'User {new_status}', 'new_status': new_status})
        
    except Exception as e:
        print(f"Error in toggle-status: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/delete-user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    print("Session contents (delete-user):", session)
    if 'loggedin' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 403
    if 'role' not in session or session['role'] != 'admin':
        return jsonify({'success': False, 'message': 'Not admin'}), 403
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check if user exists
        cur.execute('SELECT * FROM users WHERE id = %s', (user_id,))
        user = cur.fetchone()
        print("User data from DB:", user)
        
        if not user:
            return jsonify({'success': False, 'message': 'User not found'}), 404
        
        # Delete user
        cur.execute('DELETE FROM users WHERE id = %s', (user_id,))
        conn.commit()
        
        return jsonify({'success': True, 'message': 'User deleted successfully'})
        
    except Exception as e:
        print(f"Error in delete-user: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

# Add this middleware to debug all requests
@app.before_request
def before_request():
    print("\n=== Request Debug Info ===")
    print("Path:", request.path)
    print("Method:", request.method)
    print("Current session:", session)
    if 'role' in session:
        print("User role:", session['role'])
    print("========================\n")


@app.route('/stock-management')
def stock_management():
    if not check_admin():
        return redirect(url_for('login'))
    return render_template('stock_management.html')


@app.route('/stock_registration', methods=['GET', 'POST'])
def stock_registration():
    if not check_admin():
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            # Get form data
            category = request.form.get('category').strip()
            name = request.form.get('name').strip()
            description = request.form.get('description', '').strip()
            measure = request.form.get('measure').strip()
            
            # Validate required fields
            if not all([category, name, measure]):
                flash('Please fill in all required fields', 'error')
                return redirect(url_for('stock_registration'))
            
            # Validate field lengths
            if len(category) > 50:
                flash('Category name is too long (maximum 50 characters)', 'error')
                return redirect(url_for('stock_registration'))
                
            if len(name) > 100:
                flash('Item name is too long (maximum 100 characters)', 'error')
                return redirect(url_for('stock_registration'))
                
            if len(measure) > 20:
                flash('Measure unit is too long (maximum 20 characters)', 'error')
                return redirect(url_for('stock_registration'))
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Insert stock details with user information
            cur.execute('''
                INSERT INTO stocks 
                (category, name, description, measure, registered_by, registered_at) 
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (
                category,
                name,
                description,
                measure,
                session['user_id'],
                datetime.now()
            ))
            
            conn.commit()
            flash('Stock item registered successfully!', 'success')
            return redirect(url_for('stock_registration'))
            
        except Exception as e:
            print(f"Error registering stock: {e}")
            flash('An error occurred while registering the stock item', 'error')
            return redirect(url_for('stock_registration'))
        finally:
            if 'cur' in locals():
                cur.close()
            if 'conn' in locals():
                conn.close()
    
    return render_template('stock_registration.html')

@app.route('/stock_inventory')
def stock_inventory():
    if not check_admin():
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Modified query to get only the latest stock entry for each unique stock
        query = '''
            SELECT 
                s.id,
                s.category,
                s.name,
                s.description,
                s.measure,
                sq.quantity as current_quantity,
                sq.last_updated
            FROM stocks s
            LEFT JOIN stock_quantity sq ON s.id = sq.stock_id
            GROUP BY s.id, s.category, s.name, s.description, s.measure, sq.quantity, sq.last_updated
            ORDER BY s.category, s.name
        '''
        
        cur.execute(query)
        stocks = cur.fetchall()
        
        return render_template('stock_inventory.html', stocks=stocks)
        
    except Exception as e:
        print(f"Error: {e}")
        flash('Error fetching stock data', 'error')
        return render_template('stock_inventory.html', stocks=[])
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/stock_in_form/<int:stock_id>')
def stock_in_form(stock_id):
    if not check_admin():
        return redirect(url_for('login'))
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute('SELECT * FROM stocks WHERE id = %s', (stock_id,))
        stock = cur.fetchone()
        
        if not stock:
            flash('Stock not found', 'error')
            return redirect(url_for('stock_inventory'))
            
        return render_template('stock_in_form.html', stock=stock)
        
    except Exception as e:
        flash('Error loading stock details', 'error')
        return redirect(url_for('stock_inventory'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/stock_in/<int:stock_id>', methods=['POST'])
def stock_in(stock_id):
    if not check_admin():
        return redirect(url_for('login'))
    
    try:
        quantity = float(request.form['quantity'])
        unit_price = float(request.form['unit_price'])
        supplier = request.form['supplier']
        
        if quantity <= 0:
            flash('Quantity must be positive', 'error')
            return redirect(url_for('stock_in_form', stock_id=stock_id))
            
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Begin transaction
        conn.begin()
        
        # First, check if stock quantity record exists
        cur.execute('SELECT quantity FROM stock_quantity WHERE stock_id = %s', (stock_id,))
        existing_stock = cur.fetchone()
        
        if existing_stock:
            # Update existing stock quantity
            cur.execute('''
                UPDATE stock_quantity 
                SET quantity = quantity + %s,
                    last_updated = %s,
                    last_updated_by = %s
                WHERE stock_id = %s
            ''', (quantity, datetime.now(), session['user_id'], stock_id))
        else:
            # Insert new stock quantity record
            cur.execute('''
                INSERT INTO stock_quantity 
                (stock_id, quantity, last_updated, last_updated_by)
                VALUES (%s, %s, %s, %s)
            ''', (stock_id, quantity, datetime.now(), session['user_id']))
        
        # Record the stock in transaction
        cur.execute('''
            INSERT INTO stock_transactions 
            (stock_id, transaction_type, quantity, unit_price, supplier, transaction_date, recorded_by)
            VALUES (%s, 'in', %s, %s, %s, %s, %s)
        ''', (stock_id, quantity, unit_price, supplier, datetime.now(), session['user_id']))
        
        conn.commit()
        flash('Stock updated successfully', 'success')
        
    except Exception as e:
        if 'conn' in locals():
            conn.rollback()
        print(f"Error: {e}")
        flash(f'Error updating stock: {str(e)}', 'error')
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()
    
    return redirect(url_for('stock_inventory'))

@app.route('/stock_out_form/<int:stock_id>')
def stock_out_form(stock_id):
    if not check_admin():
        return redirect(url_for('login'))
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute('''
            SELECT s.*, COALESCE(sq.quantity, 0) as current_quantity 
            FROM stocks s 
            LEFT JOIN stock_quantity sq ON s.id = sq.stock_id 
            WHERE s.id = %s
        ''', (stock_id,))
        stock = cur.fetchone()
        
        if not stock:
            flash('Stock not found', 'error')
            return redirect(url_for('stock_inventory'))
            
        return render_template('stock_out_form.html', stock=stock)
        
    except Exception as e:
        flash('Error loading stock details', 'error')
        return redirect(url_for('stock_inventory'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/stock_out/<int:stock_id>', methods=['POST'])
def stock_out(stock_id):
    if not check_admin():
        return redirect(url_for('login'))
    
    try:
        quantity = float(request.form['quantity'])
        purpose = request.form['purpose']
        
        if quantity <= 0:
            flash('Quantity must be positive', 'error')
            return redirect(url_for('stock_out_form', stock_id=stock_id))
            
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check stock availability
        cur.execute('SELECT quantity FROM stock_quantity WHERE stock_id = %s', (stock_id,))
        current_stock = cur.fetchone()
        
        if not current_stock or current_stock['quantity'] < quantity:
            flash('Insufficient stock', 'error')
            return redirect(url_for('stock_out_form', stock_id=stock_id))
        
        conn.begin()
        
        # Record transaction
        cur.execute('''
            INSERT INTO stock_transactions 
            (stock_id, transaction_type, quantity, purpose, transaction_date, recorded_by)
            VALUES (%s, 'out', %s, %s, %s, %s)
        ''', (stock_id, quantity, purpose, datetime.now(), session['user_id']))
        
        # Update quantity
        cur.execute('''
            UPDATE stock_quantity 
            SET quantity = quantity - %s,
                last_updated = %s,
                last_updated_by = %s
            WHERE stock_id = %s
        ''', (quantity, datetime.now(), session['user_id'], stock_id))
        
        conn.commit()
        flash('Stock updated successfully', 'success')
        
    except Exception as e:
        if 'conn' in locals():
            conn.rollback()
        flash(f'Error updating stock: {str(e)}', 'error')
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()
    
    return redirect(url_for('stock_inventory'))

@app.route('/stock_history/<int:stock_id>')
def stock_history(stock_id):
    if not check_admin():
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get stock details
        cur.execute('SELECT * FROM stocks WHERE id = %s', (stock_id,))
        stock = cur.fetchone()
        
        if not stock:
            flash('Stock not found', 'error')
            return redirect(url_for('stock_inventory'))
        
        # Get transaction history
        cur.execute('''
            SELECT 
                t.*,
                u.name as recorded_by_name
            FROM stock_transactions t
            JOIN users u ON t.recorded_by = u.id
            WHERE t.stock_id = %s
            ORDER BY t.transaction_date DESC
        ''', (stock_id,))
        
        history = cur.fetchall()
        
        # Calculate analysis data
        cur.execute('''
            SELECT 
                COUNT(*) as total_transactions,
                SUM(CASE WHEN transaction_type = 'in' THEN quantity ELSE 0 END) as total_in,
                SUM(CASE WHEN transaction_type = 'out' THEN quantity ELSE 0 END) as total_out,
                AVG(CASE WHEN transaction_type = 'in' THEN unit_price ELSE NULL END) as avg_price,
                MAX(CASE WHEN transaction_type = 'in' THEN unit_price ELSE NULL END) as max_price,
                MIN(CASE WHEN transaction_type = 'in' THEN unit_price ELSE NULL END) as min_price
            FROM stock_transactions
            WHERE stock_id = %s
        ''', (stock_id,))
        analysis = cur.fetchone()
        
        # Get supplier analysis
        cur.execute('''
            SELECT 
                supplier,
                COUNT(*) as supply_count,
                SUM(quantity) as total_supplied,
                AVG(unit_price) as avg_price
            FROM stock_transactions
            WHERE stock_id = %s AND transaction_type = 'in' AND supplier IS NOT NULL
            GROUP BY supplier
            ORDER BY total_supplied DESC
        ''', (stock_id,))
        supplier_analysis = cur.fetchall()
        
        return render_template('stock_history.html', 
                             stock=stock, 
                             history=history, 
                             analysis=analysis,
                             supplier_analysis=supplier_analysis)
        
    except Exception as e:
        print(f"Error: {e}")
        flash('Error fetching history', 'error')
        return redirect(url_for('stock_inventory'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/stock_analysis')
def stock_analysis():
    if not check_admin():
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Overall Stock Summary
        cur.execute('''
            SELECT 
                COUNT(DISTINCT s.id) as total_stocks,
                COUNT(DISTINCT s.category) as total_categories,
                SUM(sq.quantity) as total_quantity,
                COUNT(DISTINCT st.supplier) as total_suppliers,
                SUM(CASE WHEN st.transaction_type = 'in' THEN st.quantity * st.unit_price ELSE 0 END) as total_investment
            FROM stocks s
            LEFT JOIN stock_quantity sq ON s.id = sq.stock_id
            LEFT JOIN stock_transactions st ON s.id = st.stock_id
        ''')
        summary = cur.fetchone()
        
        # Category Analysis
        cur.execute('''
            SELECT 
                s.category,
                COUNT(DISTINCT s.id) as item_count,
                SUM(sq.quantity) as total_quantity,
                SUM(CASE WHEN st.transaction_type = 'in' THEN st.quantity * st.unit_price ELSE 0 END) as investment_value,
                COUNT(DISTINCT st.supplier) as supplier_count
            FROM stocks s
            LEFT JOIN stock_quantity sq ON s.id = sq.stock_id
            LEFT JOIN stock_transactions st ON s.id = st.stock_id
            GROUP BY s.category
            ORDER BY investment_value DESC
        ''')
        category_analysis = cur.fetchall()
        
        # Recent Activity (Last 30 days)
        cur.execute('''
            SELECT 
                s.name,
                s.category,
                st.transaction_type,
                st.quantity,
                st.unit_price,
                st.supplier,
                st.purpose,
                st.transaction_date,
                u.name as recorded_by_name
            FROM stock_transactions st
            JOIN stocks s ON st.stock_id = s.id
            JOIN users u ON st.recorded_by = u.id
            WHERE st.transaction_date >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            ORDER BY st.transaction_date DESC
            LIMIT 10
        ''')
        recent_activity = cur.fetchall()
        
        # Low Stock Alerts
        cur.execute('''
            SELECT 
                s.name,
                s.category,
                sq.quantity as current_quantity,
                s.measure,
                MAX(st.transaction_date) as last_transaction_date
            FROM stocks s
            JOIN stock_quantity sq ON s.id = sq.stock_id
            LEFT JOIN stock_transactions st ON s.id = st.stock_id
            GROUP BY s.id
            HAVING current_quantity < 10
            ORDER BY current_quantity ASC
        ''')
        low_stock = cur.fetchall()
        
        # Top Suppliers Analysis
        cur.execute('''
            SELECT 
                supplier,
                COUNT(*) as transaction_count,
                SUM(quantity) as total_quantity,
                SUM(quantity * unit_price) as total_value,
                AVG(unit_price) as avg_price,
                MAX(transaction_date) as last_transaction
            FROM stock_transactions
            WHERE transaction_type = 'in' AND supplier IS NOT NULL
            GROUP BY supplier
            ORDER BY total_value DESC
            LIMIT 5
        ''')
        top_suppliers = cur.fetchall()
        
        # Monthly Transaction Summary
        cur.execute('''
            SELECT 
                DATE_FORMAT(transaction_date, '%Y-%m') as month,
                SUM(CASE WHEN transaction_type = 'in' THEN quantity ELSE 0 END) as total_in,
                SUM(CASE WHEN transaction_type = 'out' THEN quantity ELSE 0 END) as total_out,
                SUM(CASE WHEN transaction_type = 'in' THEN quantity * unit_price ELSE 0 END) as total_investment
            FROM stock_transactions
            GROUP BY month
            ORDER BY month DESC
            LIMIT 6
        ''')
        monthly_summary = cur.fetchall()
        
        # Stock Movement Patterns
        cur.execute('''
            SELECT 
                s.name,
                s.category,
                COUNT(st.id) as transaction_count,
                AVG(CASE WHEN st.transaction_type = 'out' THEN st.quantity ELSE NULL END) as avg_consumption,
                MAX(st.transaction_date) as last_movement
            FROM stocks s
            JOIN stock_transactions st ON s.id = st.stock_id
            GROUP BY s.id
            ORDER BY transaction_count DESC
            LIMIT 5
        ''')
        movement_patterns = cur.fetchall()

        return render_template('stock_analysis.html',
                             summary=summary,
                             category_analysis=category_analysis,
                             recent_activity=recent_activity,
                             low_stock=low_stock,
                             top_suppliers=top_suppliers,
                             monthly_summary=monthly_summary,
                             movement_patterns=movement_patterns)
                             
    except Exception as e:
        print(f"Analysis Error: {e}")
        flash('Error generating stock analysis', 'error')
        return redirect(url_for('stock_inventory'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/download_stock_report')
def download_stock_report():
    if not check_admin():
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get all stock data with current quantities
        cur.execute('''
            SELECT 
                s.category,
                s.name,
                s.description,
                s.measure,
                COALESCE(sq.quantity, 0) as current_quantity,
                sq.last_updated
            FROM stocks s
            LEFT JOIN stock_quantity sq ON s.id = sq.stock_id
            ORDER BY s.category, s.name
        ''')
        stocks = cur.fetchall()
        
        # Create CSV in memory
        si = StringIO()
        writer = csv.writer(si)
        writer.writerow(['Category', 'Name', 'Description', 'Measure', 'Current Quantity', 'Last Updated'])
        
        for stock in stocks:
            writer.writerow([
                stock['category'],
                stock['name'],
                stock['description'],
                stock['measure'],
                stock['current_quantity'],
                stock['last_updated'].strftime('%Y-%m-%d %H:%M') if stock['last_updated'] else 'Never'
            ])
        
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename=stock_report_{datetime.now().strftime('%Y%m%d')}.csv"
        output.headers["Content-type"] = "text/csv"
        
        return output
        
    except Exception as e:
        print(f"Export Error: {e}")
        flash('Error exporting stock report', 'error')
        return redirect(url_for('stock_analysis'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/download_transaction_report')
def download_transaction_report():
    if not check_admin():
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get all transactions
        cur.execute('''
            SELECT 
                s.category,
                s.name,
                st.transaction_type,
                st.quantity,
                st.unit_price,
                st.supplier,
                st.purpose,
                st.transaction_date,
                u.name as recorded_by
            FROM stock_transactions st
            JOIN stocks s ON st.stock_id = s.id
            JOIN users u ON st.recorded_by = u.id
            ORDER BY st.transaction_date DESC
        ''')
        transactions = cur.fetchall()
        
        # Create CSV
        si = StringIO()
        writer = csv.writer(si)
        writer.writerow(['Date', 'Category', 'Item', 'Type', 'Quantity', 'Price', 'Supplier/Purpose', 'Recorded By'])
        
        for trans in transactions:
            writer.writerow([
                trans['transaction_date'].strftime('%Y-%m-%d %H:%M'),
                trans['category'],
                trans['name'],
                trans['transaction_type'],
                trans['quantity'],
                trans['unit_price'] if trans['unit_price'] else '-',
                trans['supplier'] if trans['transaction_type'] == 'in' else trans['purpose'],
                trans['recorded_by']
            ])
        
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename=transaction_report_{datetime.now().strftime('%Y%m%d')}.csv"
        output.headers["Content-type"] = "text/csv"
        
        return output
        
    except Exception as e:
        print(f"Export Error: {e}")
        flash('Error exporting transaction report', 'error')
        return redirect(url_for('stock_analysis'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/download_supplier_report')
def download_supplier_report():
    if not check_admin():
        return redirect(url_for('login'))
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Get supplier analysis
        cur.execute('''
            SELECT 
                supplier,
                COUNT(*) as transaction_count,
                SUM(quantity) as total_quantity,
                AVG(unit_price) as avg_price,
                SUM(quantity * unit_price) as total_value,
                MIN(transaction_date) as first_transaction,
                MAX(transaction_date) as last_transaction
            FROM stock_transactions
            WHERE transaction_type = 'in' AND supplier IS NOT NULL
            GROUP BY supplier
            ORDER BY total_value DESC
        ''')
        suppliers = cur.fetchall()
        
        # Create CSV
        si = StringIO()
        writer = csv.writer(si)
        writer.writerow([
            'Supplier', 
            'Transaction Count', 
            'Total Quantity', 
            'Average Price',
            'Total Value',
            'First Transaction',
            'Last Transaction'
        ])
        
        for supplier in suppliers:
            writer.writerow([
                supplier['supplier'],
                supplier['transaction_count'],
                supplier['total_quantity'],
                f"{supplier['avg_price']:.2f}",
                f"{supplier['total_value']:.2f}",
                supplier['first_transaction'].strftime('%Y-%m-%d'),
                supplier['last_transaction'].strftime('%Y-%m-%d')
            ])
        
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename=supplier_report_{datetime.now().strftime('%Y%m%d')}.csv"
        output.headers["Content-type"] = "text/csv"
        
        return output
        
    except Exception as e:
        print(f"Export Error: {e}")
        flash('Error exporting supplier report', 'error')
        return redirect(url_for('stock_analysis'))
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

@app.route('/farm-analysis')
def farm_analysis():
    if 'loggedin' not in session or session['role'] != 'admin':
        return redirect(url_for('login'))
    return render_template('farm_analysis.html')


@app.route('/stock_audit')
def stock_audit():
    if 'loggedin' not in session or session['role'] != 'admin':
        return redirect(url_for('login'))
    
    # Fetch all records from the users table
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users")
            users = cursor.fetchall()  # Fetch all rows
    finally:
        conn.close()
    
    # Render the stock_audit.html template with the user data
    return render_template('stock_audit.html', users=users)

@app.route('/user/<int:id>')
def user_detail(id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (id,))
            user = cursor.fetchone()
    finally:
        conn.close()
    
    if not user:
        return "User not found", 404
    
    return render_template('user_detail.html', user=user)


@app.route('/feed_ingridients')
def feed_ingridients():
    # Ensure user is logged in and has admin privileges
    if 'loggedin' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Fetch all active feed formulations
            cursor.execute("""
                SELECT 
                    f.id, 
                    f.name, 
                    f.description,
                    f.created_at,
                    f.status
                FROM feed_formulations f
                WHERE f.status = 'active'
                ORDER BY f.created_at DESC
            """)
            formulations = cursor.fetchall()

            # Fetch all ingredients
            cursor.execute("""
                SELECT 
                    s.id,
                    s.category,
                    s.name,
                    s.description,
                    s.measure,
                    COALESCE(sq.quantity, 0) as quantity,
                    sq.last_updated
                FROM stocks s
                LEFT JOIN stock_quantity sq ON s.id = sq.stock_id
                WHERE UPPER(s.category) = 'INGRIDIENTS'
                ORDER BY s.name
            """)
            ingredients = cursor.fetchall()

            # Format dates safely
            for f in formulations:
                created_at = f.get('created_at')
                if isinstance(created_at, datetime):  # Check if it's a datetime object
                    f['created_at'] = created_at.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    f['created_at'] = None  # Handle non-datetime or missing values
            
            for i in ingredients:
                last_updated = i.get('last_updated')
                if isinstance(last_updated, datetime):  # Check if it's a datetime object
                    i['last_updated'] = last_updated.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    i['last_updated'] = None  # Handle non-datetime or missing values
                
    except pymysql.Error as e:
        error_message = f"Database error: {str(e)}"
        print(error_message)
        return error_message, 500
    
    except Exception as e:
        error_message = f"Unexpected error: {str(e)}"
        print(error_message)
        return error_message, 500
    
    finally:
        if 'conn' in locals() and conn:
            conn.close()
    
    return render_template('feed_ingridients.html', 
                           formulations=formulations,
                           ingredients=ingredients)


@app.route('/add_formulation', methods=['GET', 'POST'])
def add_formulation():
    if 'loggedin' not in session or session['role'] != 'admin':
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        
        if not name:
            return render_template('add_formulation.html', error="Formulation name is required")
            
        try:
            conn = get_db_connection()
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO feed_formulations 
                    (name, description, created_by, created_at, status)
                    VALUES (%s, %s, %s, NOW(), 'active')
                """, (name, description, session['user_id']))
                conn.commit()
                
            return redirect(url_for('feed_ingridients'))
                
        except pymysql.Error as e:
            print(f"Database error: {str(e)}")
            return render_template('add_formulation.html', 
                                 error="Failed to add formulation",
                                 name=name,
                                 description=description)
        finally:
            if 'conn' in locals():
                conn.close()
    
    return render_template('add_formulation.html')

@app.route('/get_formulation/<int:formulation_id>')
def get_formulation(formulation_id):
    if 'loggedin' not in session or session['role'] != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Get formulation details
            cursor.execute("""
                SELECT 
                    f.id,
                    f.name,
                    f.description,
                    f.created_at,
                    fc.stock_id,
                    fc.percentage,
                    fc.last_updated
                FROM feed_formulations f
                LEFT JOIN feed_composition fc ON f.id = fc.formulation_id
                WHERE f.id = %s AND f.status = 'active'
            """, (formulation_id,))
            
            results = cursor.fetchall()
            
            if not results:
                return jsonify({'error': 'Formulation not found'}), 404
                
            composition = []
            for row in results:
                if row['stock_id']:  # Only add if there's composition data
                    composition.append({
                        'stock_id': row['stock_id'],
                        'percentage': float(row['percentage']),
                        'last_updated': row['last_updated'].strftime('%Y-%m-%d %H:%M:%S') if row['last_updated'] else None
                    })
            
            return jsonify({
                'success': True,
                'formulation': {
                    'id': results[0]['id'],
                    'name': results[0]['name'],
                    'description': results[0]['description'],
                    'created_at': results[0]['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                },
                'composition': composition
            })
            
    except pymysql.Error as e:
        print(f"Database error: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/update_feed_percentage', methods=['POST'])
def update_feed_percentage():
    if 'loggedin' not in session or session['role'] != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    formulation_id = request.form.get('formulation_id')
    stock_id = request.form.get('stock_id')
    percentage = request.form.get('percentage')
    
    if not all([formulation_id, stock_id, percentage]):
        return jsonify({'error': 'Missing required fields'}), 400
        
    try:
        percentage = float(percentage)
        if not 0 <= percentage <= 100:
            return jsonify({'error': 'Percentage must be between 0 and 100'}), 400
    except ValueError:
        return jsonify({'error': 'Invalid percentage value'}), 400
    
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Verify formulation exists and is active
            cursor.execute("""
                SELECT id FROM feed_formulations 
                WHERE id = %s AND status = 'active'
            """, (formulation_id,))
            
            if not cursor.fetchone():
                return jsonify({'error': 'Formulation not found or inactive'}), 404
            
            # Update or insert percentage
            cursor.execute("""
                INSERT INTO feed_composition 
                    (formulation_id, stock_id, percentage, last_updated, last_updated_by)
                VALUES 
                    (%s, %s, %s, NOW(), %s)
                ON DUPLICATE KEY UPDATE 
                    percentage = VALUES(percentage),
                    last_updated = NOW(),
                    last_updated_by = VALUES(last_updated_by)
            """, (formulation_id, stock_id, percentage, session['user_id']))
            
            conn.commit()
            
            return jsonify({
                'success': True,
                'message': 'Percentage updated successfully'
            })
            
    except pymysql.Error as e:
        print(f"Database error: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        # Get form data
        name = request.form['name']
        email = request.form['email']
        phone = request.form['phone']
        code = request.form['code']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        
        # Validate input
        if not all([name, email, phone, code, password, confirm_password]):
            flash('Please fill in all fields')
            return redirect(url_for('signup'))
            
        if password != confirm_password:
            flash('Passwords do not match')
            return redirect(url_for('signup'))
            
        if not re.match(r'^\d{4}$', code):
            flash('Code must be 4 digits')
            return redirect(url_for('signup'))
        
        # Hash password
        hashed_password = generate_password_hash(password)
        
        # Insert into database
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            # Check if email or code already exists
            cur.execute('SELECT * FROM users WHERE email = %s OR code = %s', (email, code))
            account = cur.fetchone()
            
            if account:
                flash('Email or Code already exists!')
                return redirect(url_for('signup'))
                
            cur.execute('INSERT INTO users (name, email, phone, code, password) VALUES (%s, %s, %s, %s, %s)',
                       (name, email, phone, code, hashed_password))
            conn.commit()
            flash('Registration successful!')
            return redirect(url_for('login'))
            
        except Exception as e:
            print(e)
            flash('An error occurred during registration')
        finally:
            cur.close()
            conn.close()
            
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        code = request.form['code']
        password = request.form['password']
        
        conn = get_db_connection()
        cur = conn.cursor()
        
        try:
            # Include status and role in the query
            cur.execute('SELECT * FROM users WHERE code = %s', (code,))
            user = cur.fetchone()
            
            if user and check_password_hash(user['password'], password):
                # Check if user is suspended
                if user['status'] == 'suspended':
                    flash('Your account has been suspended. Please contact the administrator.')
                    return redirect(url_for('login'))
                
                # Set session variables
                session['loggedin'] = True
                session['code'] = user['code']
                session['name'] = user['name']
                session['role'] = user['role']
                session['user_id'] = user['id']  # Save the user_id to the session

                print(f"User ID in session: {session.get('user_id')}")  # Debugging line

                # Role-based redirection
                role_redirects = {
                    'admin': 'admin',
                    'employee': 'dashboard',
                    'manager': 'manager',
                    'vet': 'vet'
                }
                
                # Get the appropriate route for the user's role
                redirect_route = role_redirects.get(user['role'])
                
                if redirect_route:
                    return redirect(url_for(redirect_route))
                else:
                    flash('Invalid role assignment')
                    return redirect(url_for('login'))
            else:
                flash('Invalid code or password')
                
        except Exception as e:
            print(e)
            flash('An error occurred during login')
        finally:
            cur.close()
            conn.close()
            
    return render_template('login.html')





@app.route('/logout')
def logout():
    session.pop('loggedin', None)
    session.pop('code', None)
    session.pop('name', None)
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)