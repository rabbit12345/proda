from selenium import webdriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service
import requests
import logging
#import paho.mqtt.client as mqtt
import time
import datetime
import sys
from timeit import default_timer as timer

def proda_login():
  global currentStatus
  username = 'jaylin01'
  password = '753951Qq11'
  emailusername = 'jay'
  emailpassword = 'j1mmy.l1n'

  url = 'https://proda.humanservices.gov.au/prodalogin/pages/public/timeout.jsf'
  url = 'https://proda.humanservices.gov.au/'
  j = 1
  
  while j <= retryct:
    success = 1
    logfull('current try ct: ' + str(j))
    try:
      driver.get ('https://proda.humanservices.gov.au/')
      logfull('access proda site')
    except:
      logfull('unable to access proda site')
      success = 0

    if success == 1:
      try:
        driver.find_element(By.ID, 'loginFormAndStuff:username').send_keys(username)
        logfull('locate user name field for log in')
      except:
        logfull('unable to locate user name field for log in')
        success = 0

    if success == 1:
      try:
      	driver.find_element(By.ID, 'loginFormAndStuff:inputPassword').send_keys(password)
        logfull('locate password field')
      except:
        logfull('unable to locate password field for log in')
        success = 0

    if success == 1:
      try:        
        driver.find_element(By.ID, 'loginFormAndStuff:submitLoginWithProda').click()
        WebDriverWait(driver, 2).until(EC.title_contains('2-step verification'))
        logfull('click log in button & wait for log in to proceed to 2-step verification')
      except:
        logfull('unable to locate log in button')
        success  = 0

    if success == 1:
      try:        
        driver.find_element(By.ID, 'reselectLink').click()
        WebDriverWait(driver, 2).until(EC.title_contains("One-Time Password"))
        logfull('click getting OTP from backup channel')
      except:
        logfull('unable to locate getting OTP from backup channel')
        success  = 0
    
    if success == 1:
      try:        
        driver.find_element(By.ID, 'span-email').click()
        logfull('click getting OTP from email')
      except:
        logfull('unable to click getting OTP from email')
        success  = 0

    if success == 1:
      try:        
        driver.find_element(By.ID, 'submit-btn').click()
        WebDriverWait(driver, 2).until(EC.title_contains("2-step verification"))  
        logfull('click Next button')
      except:
        logfull('unable to click Next button')
        success  = 0

    # open new tab
    # switching tabs: driver.switch_to.window(driver.window_handles[0])
    if success == 1:
      try:
        driver.switch_to.new_window()
        driver.get('http://mail.powerun.com')
      except:
        logfull('unable to open new tab')
        success = 0

    if success == 1:
      try:        
        driver.find_element(By.ID, 'identifierId').send_keys(emailusername)
        logfull('enter email user name')
      except:
        logfull('unable to enter email user name')
        success  = 0

    if success == 1:
      try:        
        driver.find_element(By.ID, 'identifierId').send_keys(emailusername)
        logfull('enter email user name')
      except:
        logfull('unable to enter email user name')
        success  = 0






    if success == 1:
      try:
        driver.find_element_by_xpath('/html/body/form/div[4]/div[2]/div[1]/center/i').click() 
        WebDriverWait(driver, 2).until(EC.title_contains("amebconnect"))
        logfull('arrived @ 2nd page -> locate & click My Candidates button')
      except:
        logfull('unable to locate log in button')
        success = 0

    if success == 1:
    # look for target row in the browselist
      logfull('arrived @ 3rd page -> look at list of result -> row no: ' + str(currentCandidateElementId))
      try:
        candidaterow = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]')
        currentCandidateFromElement = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]/div[1]')
        candidateNameFromElement = currentCandidateFromElement.text 
        x = candidateNameFromElement.index(",")
        y = candidateNameFromElement.index("(")
        currentCandidate = (candidateNameFromElement[x+2:y-1] + ' ' + candidateNameFromElement[0:x])
        logfull('found ' + currentCandidate + ' in result list')
      except:
        logfull('unable to find row no ' + str(currentCandidateElementId) + ' in result list')
        success = 0
  
    if success == 1:
      logfull('locate status element in result list')

      escheduled = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]/div[3]/span/strong')
      currentStatus = escheduled.text
      logfull('current status : ' + currentStatus)
      if mqtt.Client.connected_flag:
        client.publish(mqttTopicResult, currentStatus, 2, True)

      if currentStatus == "Scheduled":
        scheduledtime = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]/div[3]')
        examtype = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]/div[2]')
        logfull('exam type : ' + examtype.text)
        logfull('exam status : ' + scheduledtime.text)
        if targetStatus == '':
          sendPushover(examtype.text + "\n" + scheduledtime.text, 'AMEB / ' + currentCandidate)
        else:
          if targetStatus == currentStatus:
            print(targetStatus)
            sendPushover(examtype.text + "\n" + scheduledtime.text, 'AMEB / ' + currentCandidate)
          else:
            logfull('target status is not matched: no pushover message sent')
        success = 1

      if currentStatus == "Scheduled - official notice pending":
      # try to look for exam date and time
        try:
          scheduledtime = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]/div[3]')
          examtype = driver.find_element_by_xpath('/html/body/form/div[4]/div[1]/browselistrow[' + str(currentCandidateElementId) + ']/div[1]/div[2]')
          if targetStatus == '':
            sendPushover(examtype.text + '\n' + scheduledtime.text, 'AMEB / ' + currentCandidate)
          else:
            if targetStatus == currentStatus:
              sendPushover(examtype.text + '\n' + scheduledtime.text, 'AMEB / ' + currentCandidate)
            else:
              logfull('target status is not matched: no pushover message sent')
          success = 1
        except:
          logfull('scheduled time not confirmed')
          success = 0

      if currentStatus == 'Completed':
        logfull('element Completed found: send pushover')
        sendPushover(currentCandidate + ' result may be available -> check AMEB', 'AMEB')
        success = 1

      return

    if success == 0:
      if j == retryct:
        logfull('error occured -> max retry reached --> will try again after ' + str(loopinterval) + ' min') 
        break
      else:
        logfull('error occured -> will try again --> attempt ' + str(j)) 
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

      get_prode_code = code

    finally:
      pass
    #except: 
    #  print("error")
    #  pass


#cService = webdriver.FirefoxService(executable_path=r'x:\code\python\proda\geckodriver.exe')
#driver = webdriver.Firefox(service = cService)
#driver = webdriver.Firefox('x:\code\python\proda\geckodriver.exe')
scheduled = 1
homedir = '//ds18171/Backup/raspberry/code/ameb/'
logfilename = 'ameb.log'
i = 1
loopinterval = 5
retryct = 3
terminate = 0

driver = webdriver.Firefox()
proda_login()
driver.quit()
