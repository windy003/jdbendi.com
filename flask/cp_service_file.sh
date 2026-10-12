cp jdbendi.com.service /etc/systemd/system/jdbendi.com.service
systemctl daemon-reload
systemctl enable jdbendi.com.service
systemctl start jdbendi.com.service