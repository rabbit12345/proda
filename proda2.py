from selenium import webdriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service

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

import requests
import logging
import time
import datetime
import sys
from timeit import default_timer as timer

def proda_login():
  global success
  username = 'jaylin01'
  password = '753951Qq11'

  url = 'https://proda.humanservices.gov.au/'
  j = 1
  
  while j <= retryct:
    success = 1
    logfull('current try ct: ' + str(j))

    try:
      logfull('access proda site')
      driver.get(url)      
    except:
      logfull('unable to access proda site')
      success = 0

    if success == 1:
      try:
        logfull('proda site accessed')
        driver.find_element(By.ID, 'loginFormAndStuff:username').send_keys(username)
        logfull('locate user name field for log in')
      except:
        logfull('unable to locate user name field for log in')
        success = 0

    if success == 1:
      try:
        logfull('located user name field for log in')
        driver.find_element(By.ID, 'loginFormAndStuff:inputPassword').send_keys(password)
        logfull('locate password field')
      except:
        logfull('unable to locate password field for log in')
        success = 0

    if success == 1:
      try:
        logfull('located password field')
        driver.find_element(By.ID, 'loginFormAndStuff:submitLoginWithProda').click()
        WebDriverWait(driver, 2).until(EC.title_contains('2-step verification'))
        logfull('click log in button & wait for log in to proceed to 2-step verification')
      except:
        logfull('unable to locate log in button')
        success  = 0

    if success == 1:
      try:
        logfull('click to get OTP')
        driver.find_element(By.ID, 'reselectLink').click()
        WebDriverWait(driver, 2).until(EC.title_contains("One-Time Password"))
        logfull('clicked getting OTP from backup channel')
      except:
        logfull('unable to locate getting OTP from backup channel')
        success = 0

    if success == 1:
      try:
        logfull('locate OTP from email')
        driver.find_element(By.ID, 'span-email').click()
        logfull('click getting OTP from email')
      except:
        logfull('unable to click getting OTP from email')
        success  = 0

    if success == 1:
      try:
        logfull('locate Next button')
        driver.find_element(By.ID, 'submit-btn').click()
        WebDriverWait(driver, 2).until(EC.title_contains("2-step verification"))
        logfull('clicked Next button')
      except:
        logfull('unable to click Next button')
        success = 0

    logfull('wait for 5 sec to get email from Proda')
    time.sleep(5)
    code = get_prode_code()
    print("proda code: ", code)
    success = 1

    if success == 1:
      try:
        logfull('locate Enter Code field after obtaining OTP')
        driver.find_element(By.ID, 'otppswd').send_keys(code)        
        logfull('entered one time code')
      except:
        logfull('unable to locate Enter Code field')
        success = 0

    if success == 1:
      try:
        logfull('locate and click Next button')   
        driver.find_element(By.ID, 'submit-btn').click()
        WebDriverWait(driver, 2).until(EC.title_contains("My Services"))
        logfull('clicked Next button & reached services page')
      except:
        logfull('unable to click Next button')
        success = 0

    #url2= 'https://www2.medicareaustralia.gov.au:5447/pcert/hpos/faces/landingHomeLoading.xhtml'

    #if success == 1:
    #  try:
    #    logfull('accessing final proda site')
    #    driver.get (url2)        
    #  except:
    #    logfull('unable to access final proda site')
    #    success = 0

    j = j + 1

def logfull(msg):
  # print to console 
  # log to file logger
  print(time.strftime("%d/%m/%y %H:%M:%S") + " " + msg)
  #rlog.info(msg)
  #if mqtt.Client.connected_flag:    
  #  client.publish(mqttTopicStatus, msg, 2)
  #  client.publish(mqttTopicStatusTime, time.strftime("%d/%m/%y %H:%M:%S"), 2)

def get_prode_code():
  global success
  success = 1
  code = None

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

  # connect Gmail API
  service = build('gmail', 'v1', credentials=credentials)

  # get unread emails - only from proda
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
      parts = payload.get('body')
      data = parts.get('data')
      data = data.replace("-","+").replace("_","/")

      # The Body of the message is in Encrypted format. So, we have to decode it. 
      # Get the data and decode it with base 64 decoder. 
      #msg_raw = base64.urlsafe_b64decode(data.encode('ASCII'))
      msg_raw = base64.b64decode(data) 
      msg_str = email.message_from_bytes(msg_raw)
      
      # parse with with BeautifulSoup library #
      soup = BeautifulSoup(msg_raw , "html.parser") 
      body = soup.body() 

      # find proda code
      code = soup.body.find("strong").get_text()
      
      service.users().messages().trash(userId='me', id=message['id']).execute()
      return code
      
    except: 
      print("error")
      success = 0
      pass

    finally:
      # remove proda message
      #service.users().messages().delete(userId=user_id, id=content).execute()      
      success = 1
      pass

def main():  
  proda_login()
  #driver.quit()

#cService = webdriver.FirefoxService(executable_path=r'x:\code\python\proda\geckodriver.exe')
#driver = webdriver.Firefox(service = cService)
#driver = webdriver.Firefox('x:\code\python\proda\geckodriver.exe')
driver = webdriver.Firefox()
scheduled = 1
homedir = '//ds18171/data/code/python/proda/'
logfilename = 'proda.log'
i = 1
loopinterval = 5
retryct = 1
terminate = 0

main()
