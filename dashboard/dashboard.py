# dashboard/dashboard.py
import streamlit as st
import os
from supabase import create_client
import uuid
from datetime import datetime

# Load env
from dotenv import load_dotenv
load_dotenv()

# Init Supabase
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# -------------------------------
# Page Config
# -------------------------------
st.set_page_config(
    page_title="🎟️ Event Organizer Dashboard",
    page_icon="🎟️",
    layout="centered"
)

# -------------------------------
# Session State Init
# -------------------------------
if 'user' not in st.session_state:
    st.session_state.user = None
if 'org' not in st.session_state:
    st.session_state.org = None

# -------------------------------
# Login Screen
# -------------------------------
if not st.session_state.user:
    st.title("🎟️ Organizer Login")
    phone = st.text_input("Enter your registered phone number (e.g., +23480...)", placeholder="+2348012345678")
    
    if st.button("Login"):
        if not phone.startswith("+"):
            st.error("❌ Please enter full international format (e.g., +23480...)")
        else:
            # Check if organizer exists with this phone
            org = supabase.table('organizers').select('*').eq('phone', phone).single().execute()
            if org.data:
                st.session_state.user = {"phone": phone}
                st.session_state.org = org.data
                st.success(f"✅ Welcome, {org.data['name']}!")
                st.rerun()
            else:
                st.error("❌ No organizer found with this number. Contact support.")

# -------------------------------
# Dashboard (After Login)
# -------------------------------
if st.session_state.user and st.session_state.org:
    org = st.session_state.org
    st.title(f"🎟️ {org['name']} Dashboard")
    st.caption(f"Organizer ID: {org['id']}")
    
    # -------------------------------
    # Create Event Section
    # -------------------------------
    st.header("➕ Create New Event")
    
    with st.form("create_event_form"):
        event_name = st.text_input("Event Name*", placeholder="Lagos Jazz Fest 2025")
        event_date = st.date_input("Event Date*", value=datetime.today())
        location = st.text_input("Location*", placeholder="Eko Convention Center, Lagos")
        description = st.text_area("Description (optional)")
        uploaded_file = st.file_uploader("Upload Event Poster (optional)", type=["jpg", "png", "jpeg"])
        
        # Submit button
        submitted = st.form_submit_button("Create Event")
        
        if submitted:
            if not event_name or not location:
                st.error("❌ Event Name and Location are required.")
            else:
                # Upload image if provided
                image_url = None
                if uploaded_file:
                    file_name = f"posters/{org['id']}_{uuid.uuid4().hex[:8]}.jpg"
                    supabase.storage.from_("event-posters").upload(
                        file_name,
                        uploaded_file.getvalue(),
                        file_options={"content-type": uploaded_file.type}
                    )
                    image_url = supabase.storage.from_("event-posters").get_public_url(file_name)
                
                # Insert event
                event_data = {
                    "organizer_id": org['id'],
                    "name": event_name,
                    "date": str(event_date),
                    "location": location,
                    "description": description,
                    "image_url": image_url
                }
                result = supabase.table("events").insert(event_data).execute()
                
                if result.data:
                    st.success(f"🎉 Event Created! ID: {result.data[0]['id']}")
                    st.session_state.current_event_id = result.data[0]['id']
                    st.session_state.show_ticket_types = True
                else:
                    st.error("❌ Failed to create event. Try again.")

    # -------------------------------
    # Define Ticket Types (After Event Created)
    # -------------------------------
    if 'show_ticket_types' in st.session_state and st.session_state.show_ticket_types:
        st.header("🎫 Define Ticket Types")
        st.write("Add different ticket types (e.g., VIP, General, Student) with prices and quantities.")
        
        # Allow multiple ticket types
        num_types = st.number_input("How many ticket types?", min_value=1, max_value=5, value=2)
        
        ticket_forms = []
        for i in range(num_types):
            st.subheader(f"Ticket Type {i+1}")
            col1, col2, col3 = st.columns(3)
            with col1:
                name = st.text_input(f"Name*", key=f"name_{i}", placeholder="VIP")
            with col2:
                price = st.number_input(f"Price (NGN)*", min_value=0, step=100, key=f"price_{i}")
            with col3:
                qty = st.number_input(f"Quantity*", min_value=1, step=1, key=f"qty_{i}")
            desc = st.text_input(f"Description (optional)", key=f"desc_{i}", placeholder="Access to front row")
            
            ticket_forms.append({
                "name": name,
                "price": price,
                "total_quantity": qty,
                "available_quantity": qty,
                "description": desc
            })
        
        if st.button("✅ Save Ticket Types"):
            all_valid = all(t["name"] and t["price"] >= 0 and t["total_quantity"] > 0 for t in ticket_forms)
            if not all_valid:
                st.error("❌ All ticket types must have valid name, price, and quantity.")
            else:
                # Insert all ticket types
                for t in ticket_forms:
                    t["event_id"] = st.session_state.current_event_id
                    supabase.table("ticket_types").insert(t).execute()
                
                st.success("✅ Ticket types saved!")
                
                # Generate invite link & QR
                org_code = org['code']
                invite_link = f"https://wa.me/14155238886?text=attend%20{org_code}"
                st.session_state.invite_link = invite_link
                
                st.balloons()
                st.markdown("### 📲 Your Invite Link & QR Code")
                st.code(invite_link, language="text")
                
                # Display QR
                import qrcode
                from PIL import Image
                from io import BytesIO
                
                qr = qrcode.QRCode(box_size=5, border=2)
                qr.add_data(invite_link)
                qr.make(fit=True)
                img = qr.make_image(fill='black', back_color='white')
                
                # Optional: Add logo
                # if org.get('logo_url'): ... (advanced)
                
                buf = BytesIO()
                img.save(buf, format="PNG")
                st.image(buf, caption="Scan or Share This QR Code", width=200)
                
                st.download_button(
                    "⬇️ Download QR Code",
                    buf.getvalue(),
                    file_name=f"{org_code}_invite.png",
                    mime="image/png"
                )
                
                # Clear state
                del st.session_state.show_ticket_types

    # -------------------------------
    # My Events Section
    # -------------------------------
    st.header("📊 My Events")
    events = supabase.table('events').select('*').eq('organizer_id', org['id']).execute()
    
    if events.data:
        for ev in events.data:
            with st.expander(f"🎪 {ev['name']} — {ev['date']}", expanded=False):
                st.write(f"📍 {ev['location']}")
                if ev['image_url']:
                    st.image(ev['image_url'], width=300)
                st.write(f"📝 {ev['description'] or 'No description'}")
                
                # Show ticket types
                tickets = supabase.table('ticket_types').select('*').eq('event_id', ev['id']).execute()
                if tickets.data:
                    st.subheader("🎟️ Ticket Types")
                    for t in tickets.data:
                        st.write(f"**{t['name']}** — ₦{t['price']:,} ({t['available_quantity']}/{t['total_quantity']} left)")
                else:
                    st.info("No ticket types defined yet.")
    else:
        st.info("You haven’t created any events yet. Start by creating one above!")

    # -------------------------------
    # Logout
    # -------------------------------
    if st.button("🚪 Logout"):
        st.session_state.user = None
        st.session_state.org = None
        st.rerun()