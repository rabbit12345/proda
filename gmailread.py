import os
import pickle
import base64
import email
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow 
from google.auth.transport.requests import Request 
from bs4 import BeautifulSoup

def main():
  # Load your client secrets from the downloaded JSON file
  os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
  CLIENT_SECRETS_FILE = "y:\code\python\proda\client_secret.json"
  credentials = None

  # The scopes your app will request access to
  SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

  # Start the OAuth flow
  if os.path.exists('token.pickle'):    
    # Read the token from the file and store it in the variable creds 
    with open('token.pickle', 'rb') as token: 
      credentials = pickle.load(token) 
    
  # If credentials are not available or are invalid, ask the user to log in. 
  if not credentials or not credentials.valid: 
    if credentials and credentials.expired and credentials.refresh_token: 
      credentials.refresh(Request()) 
    else: 
      flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES) 
      credentials = flow.run_local_server(port=0) 
  
    # Save the access token in token.pickle file for the next run 
    with open('token.pickle', 'wb') as token:
      pickle.dump(credentials, token) 


  #flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
  #CLIENT_SECRETS_FILE, SCOPES)

  # connect Gmail API
  service = build('gmail', 'v1', credentials=credentials)

  # get unread emails
  #unread_emails = get_emails(service, query='is:unread')
  unread_emails = service.users().messages().list(userId='me', q='from:proda@servicesaustralia.gov.au').execute()
  messages = unread_emails.get('messages')

  # iterate through all the messages 
  for message in messages: 
    # Get the message from its id 
    txt = service.users().messages().get(userId='me', id=message['id']).execute() 
    
    # Use try-except to avoid any Errors 
    try: 
      # Get value of 'payload' from dictionary 'txt' 
      payload = txt['payload']
      snippet = txt['snippet']
      headers = payload.get('headers')
      parts = payload.get('body')
      data = parts.get('data')
      data = data.replace("-","+").replace("_","/")

      # The Body of the message is in Encrypted format. So, we have to decode it. 
      # Get the data and decode it with base 64 decoder. 
      msg_raw = base64.urlsafe_b64decode(data.encode('ASCII'))
      msg_raw = base64.b64decode(data) 
      msg_str = email.message_from_bytes(msg_raw)
      
      print(data)
      print(parts)
      print(msg_str)

      # Look for Subject and Sender Email in the headers 
      for d in headers: 
        if d['name'] == 'Subject': 
          subject = d['value'] 
        if d['name'] == 'From': 
          sender = d['value'] 

      # parse with with BeautifulSoup library #
      soup = BeautifulSoup(msg_raw , "html.parser") 
      body = soup.body() 

      # find proda code
      code = soup.body.find("strong").get_text()

      # Printing the subject, sender's email and message 
      print("Subject: ", subject) 
      print("From: ", sender) 
      print("Body: ", body) 
      print('\n')
      print("Code: ", code)


    finally:
      service.users().messages().trash(userId='me', id=message['id']).execute()
      pass
      
    #except: 
    #  print("error")
    #  pass

main()