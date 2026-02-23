from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit
import client

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = 'flock-secret-key'
socketio = SocketIO(app, async_mode='threading')

chat = client.chat_client()


def on_new_message(sender, text):
    socketio.emit('new_message', {'sender': sender, 'text': text})


chat.on_message_received = on_new_message


# --- Routes ---

@app.route('/')
def index():
    if not chat.server_address:
        return redirect(url_for('servers'))
    if not chat.username:
        return redirect(url_for('register'))
    return redirect(url_for('chats'))


@app.route('/servers')
def servers():
    """Flask + SocketIO web UI glue for the chat client.

    Defines HTTP routes and socket event handlers that call into a
    `client.chat_client` instance.
    """

    return render_template('servers.html')


@app.route('/register')
def register():
    return render_template('register.html')


@app.route('/chats')
def chats():
    if not chat.username:
        return redirect(url_for('register'))
    return render_template('chats.html', username=chat.username)


@app.route('/chat/<contact>')
def private_chat(contact):
    if not chat.username:
        return redirect(url_for('register'))
    return render_template('chat.html', username=chat.username, contact=contact)


# --- SocketIO Events ---

@socketio.on('discover_servers')
def handle_discover():
    found = chat.discover_servers()
    emit('servers_found', [{'name': s[0], 'ip': s[1]} for s in found])


@socketio.on('connect_server')
def handle_connect(data):
    chat.connect_to_server((data['name'], data['ip']))
    emit('server_connected', {'name': data['name']})


@socketio.on('register_user')
def handle_register(data):
    username = data.get('username', '').strip()
    if not username or ' ' in username or '-' in username:
        emit('register_error', {'error': 'Invalid username'})
        return
    if chat.register_user(username):
        emit('registered', {'username': username})
    else:
        emit('register_error', {'error': 'Registration failed'})


@socketio.on('load_chats')
def handle_load_chats():
    previews = chat.db.get_chat_previews(chat.username)
    unread = chat.db.get_unseen_resume(chat.username)
    unread_dict = {u: c for u, c in unread}
    result = []
    for partner, last_msg in previews:
        result.append({
            'contact': partner,
            'last_message': last_msg,
            'unread': unread_dict.get(partner, 0)
        })
    emit('chats_loaded', result)


@socketio.on('load_chat_history')
def handle_load_history(data):
    contact = data['contact']
    chat.db.set_messages_as_seen(chat.username, contact)
    messages = chat.load_chat(contact)
    result = [{'id': m[0], 'author': m[1], 'receiver': m[2],
               'text': m[3], 'date': m[4]} for m in messages]
    emit('chat_history', result)


@socketio.on('send_message')
def handle_send(data):
    contact = data['contact']
    text = data['text']
    message = f"MESSAGE {chat.username} {text}"
    if not chat.send_message(contact, message):
        chat.add_to_pending_list(contact, message)
    emit('message_sent', {'contact': contact, 'text': text})


@socketio.on('mark_seen')
def handle_mark_seen(data):
    chat.db.set_messages_as_seen(chat.username, data['contact'])


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
