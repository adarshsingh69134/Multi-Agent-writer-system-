# Use an official Node.js runtime as the base image
FROM node:18-alpine AS builder

# Set the working directory inside the container
WORKDIR /app

# Copy package.json and package-lock.json (or yarn.lock) first to leverage Docker cache
COPY package*.json ./

# Install dependencies
RUN npm install --production

# Copy the rest of the application files
COPY . .

# Build the application (if applicable, e.g., for TypeScript or custom builds)
# RUN npm run build

# Use a lightweight runtime image for production
FROM node:18-alpine

# Set the working directory
WORKDIR /app

# Copy the built files (or copied files) from the builder stage
COPY --from=builder /app .

# Expose the port the app runs on (adjust if needed)
EXPOSE 8000

# Define the command to run the application
CMD ["uvicorn", "app:app","--host","0.0.0.0","--port","8000"]