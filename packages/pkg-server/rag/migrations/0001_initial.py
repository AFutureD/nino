# Generated by Django 5.0.6 on 2024-05-16 14:32

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Memory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('memory_type', models.CharField(choices=[('NOTE', 'NOTE'), ('REMINDER', 'REMINDER'), ('PIC', 'PIC')])),
                ('data', models.JSONField(null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
